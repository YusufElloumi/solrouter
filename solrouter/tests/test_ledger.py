from datetime import datetime, timedelta, timezone

import pytest

from ledger import Ledger, LedgerEntry, hash_prompt


def mk(**kw):
    d = dict(prompt_hash="h", category="code", model_id="m1", cost_usd=0.01,
             cost_is_estimated=False, latency_ms=100, success=True,
             baseline_cost_usd=0.10, attempts=1)
    d.update(kw)
    return LedgerEntry(**d)


@pytest.fixture
def ledger():
    lg = Ledger(":memory:")
    yield lg
    lg.close()


def test_hash_prompt():
    assert hash_prompt("a") == hash_prompt("a")
    assert len(hash_prompt("a")) == 64 and hash_prompt("a") != hash_prompt("b")


def test_totals_math(ledger):
    assert ledger.record(mk(cost_usd=0.01, baseline_cost_usd=0.10)) == 1
    ledger.record(mk(cost_usd=0.03, baseline_cost_usd=0.10, model_id="m2"))
    ledger.record(mk(cost_usd=0.02, baseline_cost_usd=0.50, success=False, error="boom"))
    t = ledger.totals()
    assert t["total_spent_usd"] == 0.06
    assert t["total_baseline_usd"] == 0.2
    assert t["total_saved_usd"] == 0.16
    assert t["savings_percent"] == 80.0
    assert (t["requests"], t["successes"], t["failures"]) == (3, 2, 1)
    assert t["spent_today_usd"] == 0.06


def test_zero_baseline(ledger):
    ledger.record(mk(baseline_cost_usd=0.0))
    assert ledger.totals()["savings_percent"] == 0.0
    assert Ledger(":memory:").totals()["savings_percent"] == 0.0


def test_per_model_counts_only_successes(ledger):
    ledger.record(mk(model_id="a", cost_usd=0.01))
    ledger.record(mk(model_id="a", cost_usd=0.02))
    ledger.record(mk(model_id="b", cost_usd=0.5, success=False))
    ledger.record(mk(model_id=None, success=False))
    assert ledger.totals()["per_model"] == {"a": {"calls": 2, "spent_usd": 0.03}}


def test_spent_today_excludes_yesterday(ledger):
    now = datetime.now(timezone.utc)
    ledger.record(mk(cost_usd=1.0, ts=(now - timedelta(days=1)).isoformat()))
    ledger.record(mk(cost_usd=0.25))
    ledger.record(mk(cost_usd=0.5, success=False))
    assert ledger.spent_today_usd() == 0.75
    assert ledger.spent_today_usd(now - timedelta(days=1)) == 1.0
    assert ledger.totals()["total_spent_usd"] == 1.75


def test_estimated_flag_counted(ledger):
    ledger.record(mk(cost_is_estimated=True))
    ledger.record(mk(cost_is_estimated=True))
    ledger.record(mk())
    assert ledger.totals()["estimated_cost_requests"] == 2


def test_persists_across_instances(tmp_path):
    p = str(tmp_path / "l.db")
    a = Ledger(p)
    a.record(mk(cost_usd=0.04))
    a.close()
    b = Ledger(p)
    assert b.totals()["requests"] == 1
    assert b.spent_today_usd() == 0.04
    b.close()
