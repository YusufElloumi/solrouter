"""Core ask() flow shared by api.py and cli.py: classify -> route -> execute w/ fallback -> ledger."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from classifier import classify
from executor import execute
from ledger import Ledger, LedgerEntry, hash_prompt
from router import BudgetExceeded, baseline_model, check_budget, load_models, route

log = logging.getLogger("solrouter")

MAX_PROMPT_CHARS = 20_000


class RequestRejected(Exception):
    pass


@dataclass
class AskResult:
    answer: str
    model: str
    category: str
    cost_usd: float
    cost_is_estimated: bool
    saved_usd: float
    attempts: int
    payment: str = "pay.sh-sandbox"
    tried: list = field(default_factory=list)


class SolRouter:
    def __init__(self, db_path: str | None = None, models_path: str | None = None):
        # SOLROUTER_MODELS picks the model table: models.json (local mocks, sandbox) by default,
        # models.mainnet.json for real Qwen models paid in mainnet USDC.
        self.models = load_models(models_path or os.environ.get("SOLROUTER_MODELS", "models.json"))
        self.ledger = Ledger(db_path or os.environ.get("SOLROUTER_DB", "solrouter.db"))
        self.daily_cap = float(os.environ.get("SOLROUTER_DAILY_CAP_USD", "1.00"))

    def ask(self, prompt: str, max_price_usd: float | None = None, min_tier: str | None = None) -> AskResult:
        if not prompt or not prompt.strip():
            raise RequestRejected("prompt is empty")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise RequestRejected(f"prompt exceeds {MAX_PROMPT_CHARS} chars")
        c = classify(prompt)
        try:
            candidates = route(c, self.models, max_price_usd=max_price_usd, min_tier=min_tier,
                               prompt_chars=len(prompt))
        except Exception as e:  # NoCandidateError / ValueError
            raise RequestRejected(str(e)) from e
        n = len(prompt)
        baseline_m = baseline_model(self.models, n)
        spent, tried, estimated = 0.0, [], False
        phash = hash_prompt(prompt)
        for i, m in enumerate(candidates, start=1):
            try:
                check_budget(m.estimate_usd(n), spent_so_far=spent, max_price_usd=max_price_usd)
            except BudgetExceeded as e:
                log.warning("stopping fallback: %s", e)
                break
            today = self.ledger.spent_today_usd() + spent
            if today + m.estimate_usd(n) > self.daily_cap:
                raise RequestRejected(f"daily cap ${self.daily_cap:.2f} would be exceeded (spent ${today:.4f})")
            r = execute(m, prompt)
            spent += r.cost_usd
            estimated = estimated or r.cost_is_estimated
            tried.append({"model": m.id, "ok": r.ok, "cost_usd": r.cost_usd, "latency_ms": r.latency_ms,
                          "payment": r.payment, "error": r.error})
            if r.ok:
                # Baseline: what the premium model would have cost for this call. Token-priced
                # models use this call's real token usage; flat-priced ones their list price.
                baseline = (baseline_m.cost_from_usage(*r.usage) if baseline_m.token_priced and r.usage
                            else baseline_m.estimate_usd(n))
                baseline = max(baseline, spent) if m.id == baseline_m.id else baseline
                saved = max(0.0, baseline - spent)
                self.ledger.record(LedgerEntry(phash, c.category, m.id, spent, estimated, r.latency_ms, True, baseline, i))
                return AskResult(r.text, m.id, c.category, round(spent, 6), estimated, round(saved, 6), i,
                                 r.payment, tried)
            log.error("attempt %d on %s failed: %s", i, m.id, r.error)
        self.ledger.record(LedgerEntry(phash, c.category, None, spent, estimated, 0, False, 0.0, len(tried),
                                       error="; ".join(f"{t['model']}: {t['error']}" for t in tried)[:1000]))
        raise RuntimeError(f"all {len(tried)} attempts failed: {tried}")
