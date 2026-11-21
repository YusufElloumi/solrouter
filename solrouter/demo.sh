#!/usr/bin/env bash
# One-shot SolRouter demo. Starts mock model, Pay.sh gateways and the API (if not
# already running), then runs the 5 sample prompts and prints /stats.
# Usage: ./demo.sh            (simulate mode: real 402 + quoted price, payment faked)
#        MODE=pay ./demo.sh   (real pay --sandbox curl; blocked while pay-api returns 504)
set -euo pipefail
cd "$(dirname "$0")"
PAY_BIN="${PAY_BIN:-pay}"
export PAY_BIN SOLROUTER_EXEC_MODE="${MODE:-simulate}" SOLROUTER_DB="${SOLROUTER_DB:-.run/demo.db}"
PY=.venv/bin/python
mkdir -p .run

up() { curl -s -o /dev/null "$1"; }
wait_for() { for _ in $(seq 1 60); do up "$1" && return 0; sleep 1; done; echo "timeout waiting for $1" >&2; exit 1; }

up http://127.0.0.1:8101/v1/health || { nohup $PY mock_model.py > .run/mock_model.log 2>&1 & }
wait_for http://127.0.0.1:8101/v1/health
up http://127.0.0.1:8401/v1/health || { nohup "$PAY_BIN" --sandbox gate api mock_paywall.yml --bind 127.0.0.1:8401 > .run/mock_gateway.log 2>&1 & }
wait_for http://127.0.0.1:8401/v1/health
up http://127.0.0.1:8010/stats || { nohup .venv/bin/uvicorn api:app --port 8010 > .run/api.log 2>&1 & }
wait_for http://127.0.0.1:8010/stats

echo "== 1. plain curl to a metered model endpoint (expect 402)"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST http://127.0.0.1:8401/v1/tiny -H 'content-type: application/json' -d '{"prompt":"hi"}'

echo "== 2. five prompts via cli.py (mode=$SOLROUTER_EXEC_MODE)"
LONG=$(python3 -c "print('Summarize this log. ' + 'line of server output with some details. '*120)")
for P in "What is the capital of France?" \
         "Write a python function that reverses a linked list" \
         "Why does proof of stake need slashing? Explain step by step." \
         "$LONG" \
         "[fail:tiny] what is 2+2?"; do
  echo "-- ${P:0:55}"
  $PY cli.py "$P" 2>&1 | tail -1
done

echo "== 3. budget cap (reasoning needs \$0.02, cap \$0.01)"
curl -s -X POST http://127.0.0.1:8010/ask -H 'content-type: application/json' -d '{"prompt":"Why is the sky blue?","max_price_usd":0.01}'; echo
echo "   (the API process uses the mode it was started with; cli.py above uses MODE)"

echo "== 4. GET /stats (API ledger: $SOLROUTER_DB)"
curl -s http://127.0.0.1:8010/stats | python3 -m json.tool
