"""Usage: python cli.py "your prompt" [--max-price 0.01] [--min-tier mid]"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from service import RequestRejected, SolRouter


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("prompt")
    p.add_argument("--max-price", type=float)
    p.add_argument("--min-tier", choices=["cheap", "mid", "premium"])
    p.add_argument("-v", "--verbose", action="store_true", help="print logs to stderr instead of .run/solrouter.log")
    a = p.parse_args()
    log_kw = {} if a.verbose else {"filename": os.path.join(os.path.dirname(os.path.abspath(__file__)), ".run", "solrouter.log")}
    if not a.verbose:
        os.makedirs(os.path.dirname(log_kw["filename"]), exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", **log_kw)
    try:
        r = SolRouter().ask(a.prompt, a.max_price, a.min_tier)
    except RequestRejected as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    print(r.answer)
    est = " est." if r.cost_is_estimated else ""
    path = " → ".join(t["model"] + ("" if t["ok"] else " ✗") for t in r.tried)
    pay = {"pay.sh-sandbox": "Pay.sh sandbox", "simulated": "simulated", "none": "unpaid"}.get(r.payment, r.payment)
    print(f"\n── {r.model} · {r.category} · ${r.cost_usd:.4f}{est} · saved ${r.saved_usd:.4f} · {path} · payment: {pay}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
