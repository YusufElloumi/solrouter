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
NETWORKS = {"sandbox", "mainnet"}
MAX_ATTEMPTS = 3
CHARS_PER_TOKEN_EST = 3  # conservative (real text averages ~4), so estimates err high


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
    network: str = "sandbox"  # sandbox: `pay --sandbox` (localnet test funds); mainnet: real USDC
    # Per-token pricing (USD per 1M tokens). When set, price_usd_per_call is unused: the routing
    # estimate comes from estimate_usd() and the charged cost from the response's token usage.
    in_usd_per_mtok: float | None = None
    out_usd_per_mtok: float | None = None
    max_output_tokens: int | None = None

    @property
    def token_priced(self) -> bool:
        return self.in_usd_per_mtok is not None

    def estimate_usd(self, prompt_chars: int = 0) -> float:
        """Upper-bound cost of one call: flat price, or prompt tokens (est.) + max output tokens."""
        if not self.token_priced:
            return self.price_usd_per_call
        in_tok = prompt_chars / CHARS_PER_TOKEN_EST
        return (in_tok * self.in_usd_per_mtok + self.max_output_tokens * self.out_usd_per_mtok) / 1e6

    def cost_from_usage(self, in_tokens: int, out_tokens: int) -> float:
        return (in_tokens * self.in_usd_per_mtok + out_tokens * self.out_usd_per_mtok) / 1e6


def _parse(entry: dict, idx: int) -> ModelSpec:
    mid = entry.get("id", f"#{idx}")
    def bad(msg: str) -> ValueError:
        return ValueError(f"models.json entry {mid!r}: {msg}")

    token_priced = "in_usd_per_mtok" in entry or "out_usd_per_mtok" in entry
    required = ["id", "url", "method", "request_template", "response_path", "tier", "strengths"]
    required += ["in_usd_per_mtok", "out_usd_per_mtok", "max_output_tokens"] if token_priced else ["price_usd_per_call"]
    for key in required:
        if key not in entry:
            raise bad(f"missing field {key!r}")
    if entry["tier"] not in TIER_RANK:
        raise bad(f"invalid tier {entry['tier']!r}; expected one of {sorted(TIER_RANK)}")
    strengths = entry["strengths"]
    if not isinstance(strengths, list) or not strengths or not set(strengths) <= STRENGTHS:
        raise bad(f"invalid strengths {strengths!r}; expected non-empty subset of {sorted(STRENGTHS)}")

    def positive(key: str, integer: bool = False) -> float:
        v = entry[key]
        if isinstance(v, bool) or not isinstance(v, int if integer else (int, float)) or v <= 0:
            raise bad(f"{key} must be {'an integer' if integer else 'a number'} > 0, got {v!r}")
        return v

    if token_priced:
        in_p, out_p = float(positive("in_usd_per_mtok")), float(positive("out_usd_per_mtok"))
        max_out = int(positive("max_output_tokens", integer=True))
        price = 0.0
    else:
        in_p = out_p = max_out = None
        price = float(positive("price_usd_per_call"))
    method = str(entry["method"]).upper()
    if method not in METHODS:
        raise bad(f"invalid method {entry['method']!r}")
    network = entry.get("network", "sandbox")
    if network not in NETWORKS:
        raise bad(f"invalid network {network!r}; expected one of {sorted(NETWORKS)}")
    return ModelSpec(
        id=entry["id"], url=entry["url"], method=method,
        request_template=entry["request_template"], response_path=entry["response_path"],
        price_usd_per_call=price, tier=entry["tier"], strengths=tuple(strengths),
        headers=dict(entry.get("headers") or {}), enabled=bool(entry.get("enabled", True)),
        network=network, in_usd_per_mtok=in_p, out_usd_per_mtok=out_p, max_output_tokens=max_out,
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
    prompt_chars: int = 0,
) -> list[ModelSpec]:
    if min_tier is not None and min_tier not in TIER_RANK:
        raise ValueError(f"invalid min_tier {min_tier!r}")
    required = max(classification.required_tier, min_tier or "cheap", key=TIER_RANK.__getitem__)
    floor = TIER_RANK[required]

    def ok(m: ModelSpec) -> bool:
        return (m.enabled and TIER_RANK[m.tier] >= floor
                and (max_price_usd is None or m.estimate_usd(prompt_chars) <= max_price_usd))

    key = lambda m: (m.estimate_usd(prompt_chars), TIER_RANK[m.tier], m.id)
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


def baseline_model(models: list[ModelSpec], prompt_chars: int = 0) -> ModelSpec:
    premium = [m for m in models if m.enabled and m.tier == "premium"]
    if not premium:
        raise NoCandidateError("no enabled premium model for baseline")
    return max(premium, key=lambda m: (m.estimate_usd(prompt_chars), m.id))


def check_budget(price: float, *, spent_so_far: float, max_price_usd: float | None) -> None:
    if max_price_usd is not None and spent_so_far + price > max_price_usd + 1e-12:
        raise BudgetExceeded(
            f"attempt costing {price} would exceed cap {max_price_usd} (spent {spent_so_far})"
        )
