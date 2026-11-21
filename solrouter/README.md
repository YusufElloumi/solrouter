# SolRouter

Routes each prompt to the cheapest capable model and pays every upstream call with
[Pay.sh](https://solana.com/docs/payments/pay-sh) (`pay --sandbox curl`, HTTP 402, localnet stablecoins).

```
prompt -> classifier.py -> router.py (cheapest tier>=required w/ strength, <=3 fallbacks)
       -> executor.py (pay --sandbox curl) -> ledger.py (SQLite) -> api.py / cli.py
```

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
npx @solana/pay --help          # or: brew install pay   (tested with pay 0.26.0)
```

Env vars (all optional; put them in a local `.env` you never commit):

| var | default | meaning |
|---|---|---|
| `PAY_BIN` | `pay` | path to the pay CLI |
| `SOLROUTER_EXEC_MODE` | `pay` | `pay` = real `pay --sandbox curl`; `simulate` = **fake payment**: real 402 + gateway-quoted price, settlement skipped, answer fetched from upstream; `direct` = unpaid bypass, models.json prices |
| `SOLROUTER_DAILY_CAP_USD` | `1.00` | daily spend cap across all requests |
| `SOLROUTER_DB` | `solrouter.db` | SQLite ledger |
| `SOLROUTER_CALL_TIMEOUT_S` | `60` | per-upstream-call timeout (clamped 1–300) |

## Run

```sh
# 1. mock upstream model + its paid gateway
.venv/bin/python mock_model.py                                   # :8101
pay --sandbox gate api mock_paywall.yml --bind 127.0.0.1:8401    # :8401

curl -i -X POST http://127.0.0.1:8401/v1/tiny -H 'content-type: application/json' -d '{"prompt":"hi"}'   # -> 402
pay --sandbox curl -X POST http://127.0.0.1:8401/v1/tiny -H 'content-type: application/json' -d '{"prompt":"hi"}'

# 2. SolRouter
.venv/bin/python cli.py "Write a python function that reverses a list"
.venv/bin/uvicorn api:app --port 8010
curl -X POST localhost:8010/ask -H 'content-type: application/json' -d '{"prompt":"why is the sky blue?","max_price_usd":0.05}'
curl localhost:8010/stats ; curl localhost:8010/models

# 3. SolRouter as a paid endpoint ($0.01 per /ask)
pay --sandbox gate api paywall.yml --bind 127.0.0.1:8402
pay --sandbox curl -X POST http://127.0.0.1:8402/ask -H 'content-type: application/json' -d '{"prompt":"hi"}'
```

Fallback demo: the mock returns empty text for a model named in a `[fail:<model>]` marker,
e.g. `cli.py "[fail:tiny] what is 2+2?"` → `mock-tiny✗ -> mock-mid`.

Tests: `.venv/bin/python -m pytest -q`

## Real vs mocked / estimated

- **Real:** `pay` CLI 0.26.0, `pay gate api` gateways (both YAMLs load), HTTP 402 challenges, sandbox wallet funding (100 SOL + 1000 USDC confirmed on the sandbox RPC), routing, fallback, budget caps, ledger.
- **Mocked:** all "models" (`mock_model.py` returns templated text). `placeholder-remote-llm` is a disabled placeholder; no real Pay.sh LLM endpoint was wired.
- **Estimated:** every cost. `pay curl` does not print the amount paid, so cost = `price_usd_per_call` from `models.json` (charged only when the gateway delivered a 2xx) and `cost_is_estimated=true`.

## Limitations / known issues

- **Paid retry blocked during development.** `pay --sandbox curl` rejected payment with
  "wallet … does not have enough balance on `localnet`". With `RUST_LOG=debug` the cause is
  `pay-api returned HTTP 504 Gateway Timeout: {"error":"RPC timeout after 3000ms"}`: the CLI asks
  Pay's hosted API for balances, treats the failure as zero, and refuses. The wallet *is* funded.
  `PAY_RPC_URL` did not change this. Hence `SOLROUTER_EXEC_MODE=simulate` for demos until it recovers:
  in that mode **no payment happens**; the cost shown is the price the real gateway quoted in its 402.
- "Sandbox" is not offline: it uses the hosted Surfpool RPC `https://402.surfnet.dev:8899` and `pay-api`.
- Schema discrepancy: `pay server scaffold` emits `forward_url:` + `accounting: pooled`
  (see `scaffold.paywall.yml`); the bundled docs use `routing.type: proxy`. The gateway accepts the latter, which these YAMLs use.
- `pay curl --help` without an account tries `pay setup` (keychain) instead of printing help; use `pay help curl`.
- Developed on Python 3.11 (3.12 not installed); no 3.12-only features used.
- Classifier is keyword/length rules (`classifier.Classifier` protocol is the swap point for an LLM classifier).
