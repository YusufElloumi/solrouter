import json

import pytest

from classifier import classify
from router import (MAX_ATTEMPTS, BudgetExceeded, NoCandidateError, baseline_model,
                    check_budget, load_models, route)


@pytest.fixture(scope="module")
def models():
    return load_models()


def ids(ms):
    return [m.id for m in ms]


def test_load_models(models):
    assert {"mock-tiny", "mock-mid", "mock-big"} <= set(ids(models))


def test_tier_selection(models):
    assert route(classify("hello there"), models)[0].id == "mock-tiny"
    assert route(classify("write a python function"), models)[0].id == "mock-mid"
    assert route(classify("explain why"), models)[0].id == "mock-big"


def test_ordering_by_price(models):
    r = route(classify("hello"), models)
    prices = [m.price_usd_per_call for m in r]
    assert prices == sorted(prices)


def test_disabled_never_chosen(models):
    for p in ("hello", "python bug", "explain why", "x" * 5000):
        assert "placeholder-remote-llm" not in ids(route(classify(p), models))


def test_min_tier_lifts(models):
    r = route(classify("hello"), models, min_tier="premium")
    assert ids(r) == ["mock-big"]


def test_max_price_filters(models):
    with pytest.raises(NoCandidateError, match="code"):
        route(classify("python bug"), models, max_price_usd=0.004)


def test_fallback_order_simple(models):
    assert ids(route(classify("hello"), models)) == ["mock-tiny", "mock-mid", "mock-big"]


def test_cap_attempts(models):
    assert len(route(classify("hello"), models, max_attempts=2)) == 2
    assert len(route(classify("hello"), models)) <= MAX_ATTEMPTS


def test_premium_fallback_appended_for_code(models):
    assert ids(route(classify("python bug"), models)) == ["mock-mid", "mock-big"]


def test_baseline(models):
    assert baseline_model(models).id == "mock-big"


def test_check_budget():
    check_budget(0.005, spent_so_far=0.001, max_price_usd=0.01)
    check_budget(0.005, spent_so_far=0.1, max_price_usd=None)
    with pytest.raises(BudgetExceeded):
        check_budget(0.02, spent_so_far=0.001, max_price_usd=0.01)


def test_load_models_rejects_bad_tier(tmp_path):
    entry = {"id": "x", "url": "http://x", "method": "POST", "request_template": {},
             "response_path": "a", "price_usd_per_call": 0.1, "tier": "gold",
             "strengths": ["simple"]}
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"models": [entry]}))
    with pytest.raises(ValueError, match="tier"):
        load_models(f)
    entry.update(tier="cheap", price_usd_per_call=0)
    f.write_text(json.dumps({"models": [entry]}))
    with pytest.raises(ValueError, match="price"):
        load_models(f)
