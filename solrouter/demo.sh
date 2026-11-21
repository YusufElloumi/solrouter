#!/usr/bin/env bash
# One-shot SolRouter demo. Starts mock model, Pay.sh gateways and the API (if not
# already running), then runs 5 sample prompts, a budget-cap check, and /stats.
# Usage: ./demo.sh               auto: real Pay.sh payment, simulated if Pay.sh is unavailable
#        MODE=pay ./demo.sh      real payment only (fails while pay-api returns 504)
#        MODE=simulate ./demo.sh always simulate payment (real 402 + quoted price)
set -euo pipefail
cd "$(dirname "$0")"
PAY_BIN="${PAY_BIN:-pay}"
export PAY_BIN SOLROUTER_EXEC_MODE="${MODE:-auto}" SOLROUTER_DB="${SOLROUTER_DB:-.run/demo.db}"
PY=.venv/bin/python
mkdir -p .run
B=$'\e[1m'; D=$'\e[2m'; R=$'\e[0m'

up() { curl -s -o /dev/null "$1"; }
wait_for() { for _ in $(seq 1 60); do up "$1" && return 0; sleep 1; done; echo "timeout waiting for $1" >&2; exit 1; }

up http://127.0.0.1:8101/v1/health || { nohup $PY mock_model.py > .run/mock_model.log 2>&1 & }
wait_for http://127.0.0.1:8101/v1/health
up http://127.0.0.1:8401/v1/health || { nohup "$PAY_BIN" --sandbox gate api mock_paywall.yml --bind 127.0.0.1:8401 > .run/mock_gateway.log 2>&1 & }
wait_for http://127.0.0.1:8401/v1/health
up http://127.0.0.1:8010/stats || { nohup .venv/bin/uvicorn api:app --port 8010 > .run/api.log 2>&1 & }
wait_for http://127.0.0.1:8010/stats

echo "${B}1. Unpaid request to a metered model → HTTP 402${R}"
curl -s -o /dev/null -w '   HTTP %{http_code} Payment Required\n' -X POST http://127.0.0.1:8401/v1/tiny -H 'content-type: application/json' -d '{"prompt":"hi"}'
echo

echo "${B}2. Routing five prompts${R}"
LONG=$(python3 -c "print('Summarize this log. ' + 'line of server output with some details. '*120)")
for P in "What is the capital of France?" \
         "Write a python function that reverses a linked list" \
         "Why does proof of stake need slashing? Explain step by step." \
         "$LONG" \
         "[fail:tiny] what is 2+2?"; do
  SHOWN="$P"; [ ${#P} -gt 80 ] && SHOWN="${P:0:60}… (${#P} chars)"
  echo
  echo "${B}❯ ${SHOWN}${R}"
  $PY cli.py "$P"
done
echo

echo "${B}3. Budget cap: reasoning needs \$0.02, request allows \$0.01${R}"
curl -s -X POST http://127.0.0.1:8010/ask -H 'content-type: application/json' -d '{"prompt":"Why is the sky blue?","max_price_usd":0.01}'; echo
echo

echo "${B}4. GET /stats${R}"
curl -s http://127.0.0.1:8010/stats | python3 -m json.tool
echo "${D}logs: .run/solrouter.log${R}"
