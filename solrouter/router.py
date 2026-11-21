"""Model loading and routing: cheapest capable model first, with premium fallbacks."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from classifier import Classification

TIER_RANK = {"cheap": 0, "mid": 1, "premium": 2}
STRENGTHS = {"simple", "code", "reasoning", "long_context"}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
MAX_ATTEMPTS = 3


class NoCandidateError(Exception):
    pass


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class ModelSpec:
    id: str
    url: str
    method: str
    request_template: Any
    response_path: str
    price_usd_per_call: float
    tier: str
    strengths: tuple[str, ...]
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


def _parse(entry: dict, idx: int) -> ModelSpec:
    mid = entry.get("id", f"#{idx}")
    def bad(msg: str) -> ValueError:
        return ValueError(f"models.json entry {mid!r}: {msg}")

    for key in ("id", "url", "method", "request_template", "response_path",
                "price_usd_per_call", "tier", "strengths"):
        if key not in entry:
            raise bad(f"missing field {key!r}")
    if entry["tier"] not in TIER_RANK:
        raise bad(f"invalid tier {entry['tier']!r}; expected one of {sorted(TIER_RANK)}")
    strengths = entry["strengths"]
    if not isinstance(strengths, list) or not strengths or not set(strengths) <= STRENGTHS:
        raise bad(f"invalid strengths {strengths!r}; expected non-empty subset of {sorted(STRENGTHS)}")
    price = entry["price_usd_per_call"]
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        raise bad(f"price_usd_per_call must be a number > 0, got {price!r}")
    method = str(entry["method"]).upper()
    if method not in METHODS:
        raise bad(f"invalid method {entry['method']!r}")
    return ModelSpec(
        id=entry["id"], url=entry["url"], method=method,
        request_template=entry["request_template"], response_path=entry["response_path"],
        price_usd_per_call=float(price), tier=entry["tier"], strengths=tuple(strengths),
        headers=dict(entry.get("headers") or {}), enabled=bool(entry.get("enabled", True)),
    )


def load_models(path: str | Path = "models.json") -> list[ModelSpec]:
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent / p
    data = json.loads(p.read_text())
    entries = data.get("models") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError("models.json must contain a 'models' list")
    return [_parse(e, i) for i, e in enumerate(entries)]


def route(
    classification: Classification,
    models: list[ModelSpec],
    *,
    max_price_usd: float | None = None,
    min_tier: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> list[ModelSpec]:
    if min_tier is not None and min_tier not in TIER_RANK:
        raise ValueError(f"invalid min_tier {min_tier!r}")
    required = max(classification.required_tier, min_tier or "cheap", key=TIER_RANK.__getitem__)
    floor = TIER_RANK[required]

    def ok(m: ModelSpec) -> bool:
        return (m.enabled and TIER_RANK[m.tier] >= floor
                and (max_price_usd is None or m.price_usd_per_call <= max_price_usd))

    key = lambda m: (m.price_usd_per_call, TIER_RANK[m.tier], m.id)
    chosen = sorted((m for m in models if ok(m) and classification.category in m.strengths), key=key)
    if len(chosen) < max_attempts:
        extra = [m for m in models if ok(m) and m.tier == "premium" and m not in chosen]
        chosen += sorted(extra, key=key)
    chosen = chosen[:max_attempts]
    if not chosen:
        raise NoCandidateError(
            f"no model for category={classification.category!r} tier>={required!r} "
            f"max_price_usd={max_price_usd}"
        )
    return chosen


def baseline_model(models: list[ModelSpec]) -> ModelSpec:
    premium = [m for m in models if m.enabled and m.tier == "premium"]
    if not premium:
        raise NoCandidateError("no enabled premium model for baseline")
    return max(premium, key=lambda m: (m.price_usd_per_call, m.id))


def check_budget(price: float, *, spent_so_far: float, max_price_usd: float | None) -> None:
    if max_price_usd is not None and spent_so_far + price > max_price_usd + 1e-12:
        raise BudgetExceeded(
            f"attempt costing {price} would exceed cap {max_price_usd} (spent {spent_so_far})"
        )
