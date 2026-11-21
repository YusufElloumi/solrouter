"""Prompt classification. Swap RuleClassifier for an LLM-backed one via the Classifier protocol."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

LONG_CONTEXT_CHARS = 4000

_CODE_RE = re.compile(
    r"\b(?:function|def|class|compile|regex|sql|bug|stack trace|refactor|typescript|python|rust)\b",
    re.IGNORECASE,
)
_REASONING_RE = re.compile(
    r"\b(?:why|prove|plan|explain|compare|tradeoff|derive|step by step)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class Classification:
    category: str
    required_tier: str
    reason: str


class Classifier(Protocol):
    def classify(self, prompt: str) -> Classification: ...


class RuleClassifier:
    def classify(self, prompt: str) -> Classification:
        if not prompt or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if len(prompt) >= LONG_CONTEXT_CHARS:
            return Classification(
                "long_context", "mid", f"length {len(prompt)} >= {LONG_CONTEXT_CHARS} chars"
            )
        if "```" in prompt:
            return Classification("code", "mid", "code fence found")
        if m := _CODE_RE.search(prompt):
            return Classification("code", "mid", f"code keyword '{m.group(0).lower()}'")
        if m := _REASONING_RE.search(prompt):
            return Classification("reasoning", "premium", f"reasoning keyword '{m.group(0).lower()}'")
        return Classification("simple", "cheap", "no code/reasoning/long-context signals")


_DEFAULT = RuleClassifier()


def classify(prompt: str) -> Classification:
    return _DEFAULT.classify(prompt)
