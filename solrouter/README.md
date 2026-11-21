# SolRouter

**Stop paying premium prices for easy prompts.** SolRouter reads each prompt, sends it to the
cheapest model that can handle it, and pays for the call over HTTP 402 with
[Pay.sh](https://solana.com/docs/payments/pay-sh) stablecoins on Solana. No API keys, no
subscriptions: every call is paid for individually.

![python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB)
![tests 29 passing](https://img.shields.io/badge/tests-29%20passing-2ea44f)
![payments Pay.sh / HTTP 402](https://img.shields.io/badge/payments-Pay.sh%20%C2%B7%20HTTP%20402-9945FF)

In the bundled demo, five prompts cost **$0.037 instead of $0.10** at premium-only prices:
**63% saved**, with the same answers.

```text
❯ What is the capital of France?
The capital of France is Paris.
── mock-tiny · simple · $0.0010 · saved $0.0190 · mock-tiny · payment: simulated

❯ Write a python function that reverses a linked list
Here's an iterative solution in O(n) time and O(1) space: …
── mock-mid · code · $0.0050 · saved $0.0150 · mock-mid · payment: simulated

❯ Why does proof of stake need slashing? Explain step by step.
Proof of stake needs slashing because staking alone doesn't make misbehaviour costly. …
── mock-big · reasoning · $0.0200 · saved $0.0000 · mock-big · payment: simulated

❯ [fail:tiny] what is 2+2?
2 + 2 = 4.
── mock-mid · simple · $0.0060 · saved $0.0140 · mock-tiny ✗ → mock-mid · payment: simulated
```

The last prompt shows the fallback: the cheap model returned nothing, so SolRouter moved up a
tier and still came in under the premium price.

## How it works

```mermaid
flowchart LR
    P[prompt] --> C[classifier.py<br/>simple · code · reasoning · long]
    C --> R[router.py<br/>cheapest capable model<br/>up to 3 attempts]
    R --> E[executor.py<br/>pay --sandbox curl]
    E -->|HTTP 402 → pay → retry| G[Pay.sh gateway]
    G --> M[model]
    E --> L[ledger.py<br/>SQLite spend + savings]
    L --> A[api.py · cli.py]
```

| Prompt looks like | Category | Minimum tier | Demo model | Price per call |
|---|---|---|---|---|
| Short question | `simple` | cheap | `mock-tiny` | $0.001 |
| Code keywords or a code fence | `code` | mid | `mock-mid` | $0.005 |
| 4,000+ characters | `long_context` | mid | `mock-mid` | $0.005 |
| "why", "explain", "step by step" | `reasoning` | premium | `mock-big` | $0.02 |

- **Fallback:** if a model fails or returns an empty answer, the next candidate is tried, up to three attempts.
- **Budget caps:** a per-request `max_price_usd` and a daily cap (`SOLROUTER_DAILY_CAP_USD`) reject anything that would overspend.
- **Ledger:** every call is recorded in SQLite with its cost and the premium baseline, so `GET /stats` reports the savings. Only prompt hashes are stored, never the prompts.
- **Sells itself too:** `paywall.yml` puts SolRouter's own `/ask` behind a Pay.sh gateway at $0.01 per call.

## Quick demo (2 minutes)

Needs Python 3.11+, Node 20+ and curl.

```sh
git clone https://github.com/YusufElloumi/solrouter.git
cd solrouter/solrouter

# 1. Python deps
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Pay.sh CLI (pick one)
npm install -g @solana/pay      # puts `pay` on PATH (downloads the native binary on first run)
# brew install pay

# 3. Run the whole demo
./demo.sh
```

`demo.sh` starts everything it needs in the background (mock model on :8101, its Pay.sh
gateway on :8401, the SolRouter API on :8010), then:

1. sends an unpaid request to a metered model and shows the **HTTP 402**,
2. routes five prompts (simple, code, reasoning, long input, forced fallback) and prints each
   answer with a summary line: `── model · category · cost · saved · attempts · payment: …`,
3. shows the per-request **budget cap** rejecting a prompt,
4. prints **`GET /stats`** (total spent, saved vs premium, savings %, per-model usage).

The first run takes about 15 seconds while the sandbox gateway funds its wallets. Variants:

```sh
rm -f .run/demo.db .run/pay_down   # fresh ledger + retry real payment
MODE=simulate ./demo.sh            # always simulate payment
MODE=pay ./demo.sh                 # real Pay.sh payment only (see Limitations)
PAY_BIN=/path/to/pay ./demo.sh     # if `pay` isn't on PATH
.venv/bin/python cli.py -v "your prompt"   # one prompt, logs to the terminal
.venv/bin/python -m pytest -q      # tests
```

Stop everything: `pkill -f mock_model.py; pkill -f "gate api"; pkill -f "uvicorn api:app"`

### What `payment:` means

The `payment:` field on each summary line reports what actually happened:

- `Pay.sh sandbox`: a real sandbox payment went through.
- `simulated`: Pay.sh was unavailable, so the price quoted in the real 402 was recorded but
  **no payment was sent**. The sample output above is from a simulated run.
- `unpaid`: `direct` mode, which bypasses the gateway.

Logs go to `.run/solrouter.log`.

## Use it

```sh
# CLI
.venv/bin/python cli.py "Write a python function that reverses a list"
.venv/bin/python cli.py "Why is the sky blue?" --max-price 0.05 --min-tier mid

# HTTP API
.venv/bin/uvicorn api:app --port 8010
curl -X POST localhost:8010/ask -H 'content-type: application/json' \
  -d '{"prompt":"why is the sky blue?","max_price_usd":0.05}'
curl localhost:8010/stats ; curl localhost:8010/models
```

| Endpoint | What it does |
|---|---|
| `POST /ask` | `{prompt, max_price_usd?, min_tier?}` → answer, model, category, cost, saved, attempts, payment |
| `GET /stats` | total spent, premium baseline, saved, savings %, per-model usage, spent today |
| `GET /models` | the model table from `models.json` |

### Run the pieces by hand

```sh
# 1. mock upstream model + its paid gateway
.venv/bin/python mock_model.py                                   # :8101
pay --sandbox gate api mock_paywall.yml --bind 127.0.0.1:8401    # :8401

curl -i -X POST http://127.0.0.1:8401/v1/tiny -H 'content-type: application/json' -d '{"prompt":"hi"}'   # -> 402
pay --sandbox curl -X POST http://127.0.0.1:8401/v1/tiny -H 'content-type: application/json' -d '{"prompt":"hi"}'

# 2. SolRouter as a paid endpoint ($0.01 per /ask)
pay --sandbox gate api paywall.yml --bind 127.0.0.1:8402
pay --sandbox curl -X POST http://127.0.0.1:8402/ask -H 'content-type: application/json' -d '{"prompt":"hi"}'
```

Fallback demo: the mock returns empty text for a model named in a `[fail:<model>]` marker,
e.g. `cli.py "[fail:tiny] what is 2+2?"` → `mock-tiny ✗ → mock-mid`.

## Configuration

All optional; put them in a local `.env` you never commit. Tested with `pay` 0.26.0.

| var | default | meaning |
|---|---|---|
| `PAY_BIN` | `pay` | path to the pay CLI |
| `SOLROUTER_EXEC_MODE` | `auto` | `auto` = try real payment, fall back to `simulate` for 5 min if Pay.sh fails; `pay` = real `pay --sandbox curl` only; `simulate` = **fake payment**: real 402 + gateway-quoted price, settlement skipped, answer fetched from upstream; `direct` = unpaid bypass, models.json prices |
| `SOLROUTER_DAILY_CAP_USD` | `1.00` | daily spend cap across all requests |
| `SOLROUTER_DB` | `solrouter.db` | SQLite ledger |
| `SOLROUTER_CALL_TIMEOUT_S` | `60` | per-upstream-call timeout (clamped 1–300) |

To add a model, add an entry to `models.json` (URL, request template, response path, price,
tier, strengths). To swap the keyword classifier for an LLM one, implement the
`classifier.Classifier` protocol.

## Real vs mocked / estimated

- **Real:** `pay` CLI 0.26.0, `pay gate api` gateways (both YAMLs load), HTTP 402 challenges, sandbox wallet funding (100 SOL + 1000 USDC confirmed on the sandbox RPC), routing, fallback, budget caps, ledger.
- **Mocked:** all "models" (`mock_model.py` returns canned answers for the demo prompts and generic text otherwise). `placeholder-remote-llm` is a disabled placeholder; no real Pay.sh LLM endpoint was wired.
- **Estimated:** costs in `pay` mode. `pay curl` does not print the amount paid, so cost = `price_usd_per_call` from `models.json` (charged only when the gateway delivered a 2xx) and `cost_is_estimated=true`. In `simulate` mode the cost is the price the gateway quoted in its 402.

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
