"""Usage: python cli.py "your prompt" [--max-price 0.01] [--min-tier mid]"""

from __future__ import annotations

import argparse
import logging
import sys

from service import RequestRejected, SolRouter


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("prompt")
    p.add_argument("--max-price", type=float)
    p.add_argument("--min-tier", choices=["cheap", "mid", "premium"])
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        r = SolRouter().ask(a.prompt, a.max_price, a.min_tier)
    except RequestRejected as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    print(r.answer)
    est = " (estimated)" if r.cost_is_estimated else ""
    path = " -> ".join(t["model"] + ("" if t["ok"] else "✗") for t in r.tried)
    print(f"--\nmodel={r.model} category={r.category} cost=${r.cost_usd:.4f}{est} saved=${r.saved_usd:.4f} attempts={r.attempts} [{path}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
