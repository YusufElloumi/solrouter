"""Local stand-in for Pay.sh's hosted balance API (pay-api).

Why: `pay --sandbox curl` asks pay-api for the wallet's stablecoin balances before it
will sign a payment. The hosted host (pay-api.gateway-402.com) currently does not
resolve (NXDOMAIN), so the CLI sees "no balance" and refuses to pay, even though the
sandbox wallet holds real (sandbox) USDC on the Surfpool RPC.

This shim answers the one endpoint the CLI uses for that check,
    GET /v1/balance/stablecoins?address=<pubkey>&network=sandbox
by reading token balances straight from Solana RPC (sandbox: the Surfpool RPC; mainnet:
SHIM_MAINNET_RPC_URL, default the public mainnet RPC). Point the CLI at it with
    PAY_API_URL=http://127.0.0.1:8498
Payments themselves are still signed by `pay` and settled on chain; this only replaces
the balance lookup (it cannot move funds). Read-only, binds to localhost, no secrets.

Run: .venv/bin/python pay_api_shim.py   (env: SHIM_PORT, PAY_RPC_URL, SHIM_MAINNET_RPC_URL)
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

RPC_URL = os.environ.get("PAY_RPC_URL", "https://402.surfnet.dev:8899")
MAINNET_RPC_URL = os.environ.get("SHIM_MAINNET_RPC_URL", "https://api.mainnet-beta.solana.com")
RPC_BY_NETWORK = {"sandbox": RPC_URL, "localnet": RPC_URL, "mainnet": MAINNET_RPC_URL}
PORT = int(os.environ.get("SHIM_PORT", "8498"))
# Stablecoins the gateways advertise (mainnet mints, mirrored on the Surfpool fork).
SYMBOLS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "CASHx9KJUStyftLFWGvEVf59SGeG9sh5FfcnZMVPCASH": "CASH",
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo": "PYUSD",
    "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH": "USDG",
}
BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
RPC_TIMEOUT_S = 2.5
FIRST_FETCH_TIMEOUT_S = 8.0  # cold owner: worth waiting; the CLI retries balances later anyway
CACHE_TTL_S = 10.0
_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}  # (network, owner) -> (fetched_at, balances)
_pool = ThreadPoolExecutor(max_workers=8)

log = logging.getLogger("solrouter.pay_api_shim")


def _ssl_context() -> ssl.SSLContext:
    # python.org macOS builds ship without a CA bundle; prefer certifi, then the system one.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        if os.path.exists("/etc/ssl/cert.pem"):
            return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
        return ssl.create_default_context()


_SSL = _ssl_context()


def _rpc(rpc_url: str, method: str, params: list) -> dict:
    req = urllib.request.Request(
        rpc_url, method="POST", headers={"content-type": "application/json"},
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
    )
    with urllib.request.urlopen(req, timeout=RPC_TIMEOUT_S, context=_SSL) as r:
        out = json.loads(r.read(2_000_000))
    if "error" in out:
        raise RuntimeError(out["error"])
    return out["result"]


def _mint_balance(network: str, owner: str, mint: str) -> dict | None:
    res = _rpc(RPC_BY_NETWORK[network], "getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
    raw, decimals = 0, None
    for acct in res["value"]:
        amt = acct["account"]["data"]["parsed"]["info"]["tokenAmount"]
        raw += int(amt["amount"])
        decimals = amt["decimals"]
    if decimals is None:
        return None
    return {"mint": mint, "symbol": SYMBOLS[mint], "raw_amount": str(raw), "ui_amount": raw / 10 ** decimals}


def _fetch(network: str, owner: str, timeout_s: float) -> list[dict]:
    futures = {_pool.submit(_mint_balance, network, owner, m): m for m in SYMBOLS}
    deadline = time.monotonic() + timeout_s
    out, failed = [], 0
    for fut, mint in futures.items():
        try:
            b = fut.result(timeout=max(0.0, deadline - time.monotonic()))
            if b:
                out.append(b)
        except Exception as e:  # timeout, RPC error, mint missing on fork
            failed += 1
            log.debug("skip %s: %s", SYMBOLS[mint], e)
    if failed == len(SYMBOLS):
        raise RuntimeError(f"{network} RPC did not answer any balance query")
    _cache[(network, owner)] = (time.monotonic(), out)
    return out


def stablecoin_balances(network: str, owner: str) -> list[dict]:
    """Balances for owner, answered well inside the CLI's ~3 s pay-api timeout.

    The sandbox RPC takes 0.3-2.5 s per call, so mints are queried in parallel (per mint:
    Surfpool rejects a Token-2022 programId filter) and owners the CLI has asked about are
    kept warm by _refresher. A cached answer is served immediately (stale-while-revalidate);
    only a never-seen owner waits on the RPC."""
    hit = _cache.get((network, owner))
    if hit:
        if time.monotonic() - hit[0] > CACHE_TTL_S:
            _pool.submit(_safe_fetch, network, owner)
        return hit[1]
    return _fetch(network, owner, FIRST_FETCH_TIMEOUT_S)


def _safe_fetch(network: str, owner: str) -> None:
    try:
        _fetch(network, owner, RPC_TIMEOUT_S * 2)
    except Exception as e:
        log.warning("background refresh failed for %s %s: %s", network, owner, e)


def _refresher() -> None:
    while True:
        time.sleep(CACHE_TTL_S)
        for network, owner in list(_cache):
            _safe_fetch(network, owner)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        u = urlsplit(self.path)
        if u.path == "/health":
            return self._send(200, {"ok": True, "rpc": RPC_BY_NETWORK})
        if u.path != "/v1/balance/stablecoins":
            return self._send(404, {"error": "not found"})
        q = parse_qs(u.query)
        address, network = q.get("address", [""])[0], q.get("network", [""])[0]
        if network not in RPC_BY_NETWORK:
            return self._send(400, {"error": f"unsupported network {network!r}"})
        if not BASE58.match(address):
            return self._send(400, {"error": "invalid address"})
        try:
            balances = stablecoin_balances(network, address)
        except Exception as e:  # RPC down / malformed
            log.error("balance lookup failed for %s: %s", address, e)
            return self._send(502, {"error": f"rpc error: {e}"})
        log.info("balances %s %s -> %s", network, address, [(b["symbol"], b["ui_amount"]) for b in balances])
        self._send(200, {"balances": balances})

    def log_message(self, fmt: str, *args) -> None:
        log.debug(fmt, *args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("pay-api shim on 127.0.0.1:%d -> %s", PORT, RPC_BY_NETWORK)
    threading.Thread(target=_refresher, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
