"""Execute one upstream model call, paying via the Pay.sh CLI.

Mode (env SOLROUTER_EXEC_MODE):
  pay    (default) `pay --sandbox curl ...` -> handles 402, pays on localnet, retries.
  direct UNPAID dev mode: plain HTTP straight to the upstream behind the gateway
         (SOLROUTER_DIRECT_BASE, default http://127.0.0.1:8101). Exists only because
         the hosted pay-api balance check was returning 504 during development.

The request body is passed on stdin (`--data-binary @-`), never as an argv string,
and no shell is used. Cost: the pay CLI does not print the amount paid in its
curl passthrough output, so cost is price_usd_per_call from models.json and is
marked estimated. A call is only charged if the gateway delivered (HTTP 2xx).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from router import ModelSpec

log = logging.getLogger("solrouter.executor")

MAX_OUTPUT_BYTES = 1_000_000
STATUS_MARK = "\n__SOLROUTER_STATUS__:"


@dataclass
class ExecResult:
    ok: bool
    text: str
    cost_usd: float
    cost_is_estimated: bool
    latency_ms: int
    http_status: int | None
    error: str | None = None


def _build_body(template: Any, prompt: str) -> Any:
    if isinstance(template, str):
        return template.replace("{prompt}", prompt)
    if isinstance(template, list):
        return [_build_body(v, prompt) for v in template]
    if isinstance(template, dict):
        return {k: _build_body(v, prompt) for k, v in template.items()}
    return template


def extract(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(path)
    return cur


def _finish(model: ModelSpec, status: int | None, raw: bytes, t0: float, err: str | None) -> ExecResult:
    latency = int((time.monotonic() - t0) * 1000)
    delivered = status is not None and 200 <= status < 300
    cost = model.price_usd_per_call if delivered else 0.0
    if err:
        return ExecResult(False, "", cost, True, latency, status, err)
    if not delivered:
        return ExecResult(False, "", cost, True, latency, status, f"HTTP {status}: {raw[:300]!r}")
    try:
        text = extract(json.loads(raw), model.response_path)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return ExecResult(False, "", cost, True, latency, status, f"bad response ({e}): {raw[:300]!r}")
    if not isinstance(text, str) or not text.strip():
        return ExecResult(False, "", cost, True, latency, status, "empty answer")
    return ExecResult(True, text, cost, True, latency, status)


def _call_pay(model: ModelSpec, body: bytes, timeout_s: float) -> ExecResult:
    pay_bin = os.environ.get("PAY_BIN", "pay")
    args = [pay_bin, "--sandbox", "curl", "-sS", "--max-time", str(int(timeout_s)),
            "-X", model.method]
    for k, v in model.headers.items():
        args += ["-H", f"{k}: {v}"]
    args += ["--data-binary", "@-", "-w", STATUS_MARK + "%{http_code}", model.url]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(args, input=body, capture_output=True, timeout=timeout_s + 30)
    except subprocess.TimeoutExpired:
        log.error("pay curl timed out for %s", model.id)
        return ExecResult(False, "", 0.0, True, int((time.monotonic() - t0) * 1000), None, "timeout")
    except FileNotFoundError:
        return ExecResult(False, "", 0.0, True, 0, None, f"pay CLI not found at {pay_bin!r} (set PAY_BIN)")
    out = proc.stdout[:MAX_OUTPUT_BYTES]
    if len(proc.stdout) > MAX_OUTPUT_BYTES:
        return _finish(model, None, b"", t0, "upstream output exceeded size limit")
    raw, _, status_s = out.decode("utf-8", "replace").rpartition(STATUS_MARK)
    status = int(status_s) if status_s.strip().isdigit() and status_s.strip() != "000" else None
    if proc.returncode != 0:
        err = proc.stderr[:2000].decode("utf-8", "replace").strip()
        log.error("pay curl failed for %s (exit %s): %s", model.id, proc.returncode, err)
        return _finish(model, status, raw.encode(), t0, f"pay exit {proc.returncode}: {err}")
    return _finish(model, status, raw.encode(), t0, None)


def _call_direct(model: ModelSpec, body: bytes, timeout_s: float) -> ExecResult:
    base = urlsplit(os.environ.get("SOLROUTER_DIRECT_BASE", "http://127.0.0.1:8101"))
    u = urlsplit(model.url)
    url = urlunsplit((base.scheme, base.netloc, u.path, u.query, ""))
    req = urllib.request.Request(url, data=body, method=model.method, headers=model.headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return _finish(model, r.status, r.read(MAX_OUTPUT_BYTES + 1)[:MAX_OUTPUT_BYTES], t0, None)
    except urllib.error.HTTPError as e:
        return _finish(model, e.code, e.read(2000), t0, None)
    except (urllib.error.URLError, TimeoutError) as e:
        log.error("direct call failed for %s: %s", model.id, e)
        return _finish(model, None, b"", t0, f"connection error: {e}")


def _call_simulate(model: ModelSpec, body: bytes, timeout_s: float) -> ExecResult:
    """FAKE payment: real 402 challenge from the gateway, simulated settlement.

    1. POST to the real gateway without payment -> must get HTTP 402; read the
       advertised price from its JSON body (pricing.dimensions[0].price_usd).
    2. Pretend to pay (no transaction is sent), then fetch the answer from the
       upstream directly (as the gateway would after verifying payment).
    """
    req = urllib.request.Request(model.url, data=body, method=model.method, headers=model.headers)
    price = None
    try:
        urllib.request.urlopen(req, timeout=timeout_s).close()
        log.warning("%s did not return 402; endpoint is free", model.id)
        price = 0.0
    except urllib.error.HTTPError as e:
        if e.code != 402:
            return _finish(model, e.code, e.read(2000), time.monotonic(), None)
        try:
            price = float(json.loads(e.read(10_000))["pricing"]["dimensions"][0]["price_usd"])
        except (ValueError, KeyError, IndexError, TypeError) as pe:
            log.error("could not parse 402 pricing for %s: %s", model.id, pe)
    except (urllib.error.URLError, TimeoutError) as e:
        return ExecResult(False, "", 0.0, True, 0, None, f"gateway unreachable: {e}")
    log.info("SIMULATED payment of $%s to %s (no on-chain tx)", price, model.id)
    r = _call_direct(model, body, timeout_s)
    if price is not None and r.http_status is not None and 200 <= r.http_status < 300:
        r.cost_usd, r.cost_is_estimated = price, False  # price is gateway-quoted, payment is simulated
    return r


def execute(model: ModelSpec, prompt: str, timeout_s: float | None = None) -> ExecResult:
    timeout_s = timeout_s or float(os.environ.get("SOLROUTER_CALL_TIMEOUT_S", "60"))
    timeout_s = max(1.0, min(timeout_s, 300.0))
    body = json.dumps(_build_body(model.request_template, prompt)).encode()
    mode = os.environ.get("SOLROUTER_EXEC_MODE", "pay")
    if mode == "simulate":
        return _call_simulate(model, body, timeout_s)
    if mode == "direct":
        log.warning("UNPAID direct mode: calling %s without Pay.sh", model.id)
        return _call_direct(model, body, timeout_s)
    return _call_pay(model, body, timeout_s)
