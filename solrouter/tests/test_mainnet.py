"""Real (mainnet, token-priced) models: routing, cost math, and the executor's safety rules.
No network: subprocess.run is replaced, so nothing is ever paid."""
import json
import subprocess

import pytest

import executor
from classifier import classify
from router import baseline_model, load_models, route


@pytest.fixture(scope="module")
def models():
    return load_models("models.mainnet.json")


def ids(ms):
    return [m.id for m in ms]


def test_mainnet_table_is_all_mainnet_and_token_priced(models):
    assert models and all(m.network == "mainnet" and m.token_priced for m in models)
    assert all(m.request_template["max_tokens"] == m.max_output_tokens for m in models)


def test_mock_table_stays_sandbox():
    assert all(m.network == "sandbox" and not m.token_priced for m in load_models())


def test_routing_picks_cheapest_capable(models):
    assert route(classify("hello there"), models)[0].id == "qwen3.5-flash"
    assert route(classify("write a python function"), models)[0].id == "qwen3-coder-flash"
    assert route(classify("explain why"), models)[0].id == "qwen3.8-max"
    long = "x" * 5000
    assert route(classify(long), models, prompt_chars=len(long))[0].id == "qwen3.5-plus"
    assert baseline_model(models).id == "qwen3.8-max"


def test_estimate_and_usage_cost(models):
    flash = next(m for m in models if m.id == "qwen3.5-flash")
    # 300 chars ≈ 100 tokens in (conservative /3) + 1024 max out
    assert flash.estimate_usd(300) == pytest.approx((100 * 0.10 + 1024 * 0.40) / 1e6)
    assert flash.cost_from_usage(20, 10) == pytest.approx((20 * 0.10 + 10 * 0.40) / 1e6)
    assert flash.cost_from_usage(20, 10) < flash.estimate_usd(60)


def test_max_price_uses_estimate(models):
    r = route(classify("hello"), models, max_price_usd=0.001)
    assert ids(r)[0] == "qwen3.5-flash" and "qwen3.8-max" not in ids(r)


def test_token_priced_entry_requires_max_output(tmp_path):
    entry = {"id": "x", "url": "https://x", "method": "POST", "request_template": {},
             "response_path": "a", "in_usd_per_mtok": 1, "out_usd_per_mtok": 1,
             "tier": "cheap", "strengths": ["simple"], "network": "mainnet"}
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"models": [entry]}))
    with pytest.raises(ValueError, match="max_output_tokens"):
        load_models(f)
    entry.update(max_output_tokens=10, network="devnet")
    f.write_text(json.dumps({"models": [entry]}))
    with pytest.raises(ValueError, match="network"):
        load_models(f)


def _fake_pay(monkeypatch, *, stdout=b"", returncode=0, stderr=b""):
    calls = []

    def run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    monkeypatch.setattr(executor.subprocess, "run", run)
    return calls


def test_mainnet_call_omits_sandbox_and_costs_from_usage(models, monkeypatch):
    monkeypatch.setenv("SOLROUTER_EXEC_MODE", "auto")
    body = {"choices": [{"message": {"content": "Paris."}}], "usage": {"prompt_tokens": 20, "completion_tokens": 3}}
    calls = _fake_pay(monkeypatch, stdout=json.dumps(body).encode() + executor.STATUS_MARK.encode() + b"200")
    flash = next(m for m in models if m.id == "qwen3.5-flash")
    r = executor.execute(flash, "What is the capital of France?")
    assert r.ok and r.text == "Paris." and r.payment == "pay.sh-mainnet"
    assert "--sandbox" not in calls[0] and calls[0][1] == "curl"
    assert r.cost_usd == pytest.approx(flash.cost_from_usage(20, 3))


def test_mainnet_missing_usage_charges_upper_bound(models, monkeypatch):
    monkeypatch.setenv("SOLROUTER_EXEC_MODE", "pay")
    body = {"choices": [{"message": {"content": "ok"}}]}
    _fake_pay(monkeypatch, stdout=json.dumps(body).encode() + executor.STATUS_MARK.encode() + b"200")
    flash = next(m for m in models if m.id == "qwen3.5-flash")
    assert executor.execute(flash, "hi").cost_usd == pytest.approx(flash.estimate_usd(2))


@pytest.mark.parametrize("mode", ["simulate", "direct"])
def test_mainnet_refuses_sandbox_only_modes(models, monkeypatch, mode):
    monkeypatch.setenv("SOLROUTER_EXEC_MODE", mode)
    calls = _fake_pay(monkeypatch)
    r = executor.execute(models[0], "hi")
    assert not r.ok and "sandbox-only" in r.error and calls == []


def test_mainnet_payment_failure_is_not_simulated(models, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLROUTER_EXEC_MODE", "auto")
    monkeypatch.setattr(executor, "PAY_DOWN_FILE", str(tmp_path / "pay_down"))
    _fake_pay(monkeypatch, returncode=1, stderr=b"Payment rejected: not enough balance on mainnet")
    r = executor.execute(models[0], "hi")
    assert not r.ok and r.payment == "pay.sh-mainnet" and r.cost_usd == 0.0
    assert not (tmp_path / "pay_down").exists()


def test_sandbox_model_still_uses_sandbox_flag(monkeypatch):
    monkeypatch.setenv("SOLROUTER_EXEC_MODE", "pay")
    body = {"output": {"text": "hi"}}
    calls = _fake_pay(monkeypatch, stdout=json.dumps(body).encode() + executor.STATUS_MARK.encode() + b"200")
    tiny = next(m for m in load_models() if m.id == "mock-tiny")
    assert executor.execute(tiny, "hi").ok
    assert calls[0][1:3] == ["--sandbox", "curl"]
