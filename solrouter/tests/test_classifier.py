import pytest

from classifier import Classification, RuleClassifier, classify


def test_simple():
    c = classify("What is the capital of France?")
    assert (c.category, c.required_tier) == ("simple", "cheap")


def test_code_keyword_and_fence():
    assert classify("Write a Python script").category == "code"
    assert classify("fix this:\n```\nx=1\n```").category == "code"
    assert classify("Fix the BUG").required_tier == "mid"


def test_reasoning():
    c = classify("Explain the tradeoff between A and B")
    assert (c.category, c.required_tier) == ("reasoning", "premium")


def test_word_boundaries():
    assert classify("The classic whyte house").category == "simple"
    assert classify("Hello subclass").category == "simple"


def test_long_context_priority():
    c = classify("why " + "x" * 4000)
    assert (c.category, c.required_tier) == ("long_context", "mid")
    assert "length" in c.reason


def test_code_beats_reasoning():
    assert classify("why does this function fail").category == "code"


def test_reason_names_rule():
    assert "regex" in classify("need a regex").reason


@pytest.mark.parametrize("p", ["", "   \n\t"])
def test_empty_raises(p):
    with pytest.raises(ValueError):
        classify(p)


def test_class_and_frozen():
    c = RuleClassifier().classify("hi")
    assert isinstance(c, Classification)
    with pytest.raises(Exception):
        c.category = "x"
