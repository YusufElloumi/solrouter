"""Tiny local upstream "LLM" used for offline demos.

It is NOT a real model: answers are deterministic templates. It sits behind
`pay --sandbox gate api mock_paywall.yml`, which charges per call, so the
full HTTP 402 -> pay -> retry flow can be exercised with zero external accounts.

Endpoints (all POST, JSON body {"prompt": str}):
  /v1/tiny     cheap model
  /v1/mid      mid-tier model
  /v1/big      premium model
  GET /v1/health

Fault injection (demo only): if the prompt contains the marker "[fail:<model>]",
that model returns an empty answer, so the router's fallback can be shown
deterministically, e.g. "[fail:tiny] what is 2+2?".
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("mock_model")
app = FastAPI(title="SolRouter mock model")

MAX_PROMPT_CHARS = 20_000


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)


# Canned, realistic-looking answers for the demo prompts (keyword -> answer).
CANNED = [
    (("capital", "france"), "The capital of France is Paris."),
    (("2+2",), "2 + 2 = 4."),
    (("reverse", "linked list"),
     "Here's an iterative solution in O(n) time and O(1) space:\n\n"
     "```python\nclass Node:\n    def __init__(self, val, next=None):\n        self.val, self.next = val, next\n\n"
     "def reverse_list(head: Node | None) -> Node | None:\n    prev = None\n    while head:\n"
     "        head.next, prev, head = prev, head, head.next\n    return prev\n```\n\n"
     "Each step re-points one node's `next` to the previous node, so after one pass `prev` is the new head."),
    (("slashing",),
     "Proof of stake needs slashing because staking alone doesn't make misbehaviour costly.\n\n"
     "1. **Nothing at stake:** signing two conflicting blocks is free in PoS (unlike PoW, where work is spent), "
     "so validators could vote on every fork.\n"
     "2. **Accountability:** equivocation leaves cryptographic evidence (two signatures for the same slot), "
     "so the protocol can prove who cheated.\n"
     "3. **Economic security:** slashing burns part of the offender's stake, so attacking finality costs "
     "a known fraction of total stake (e.g. ≥1/3 slashed for a safety violation).\n"
     "4. **Long-range attacks:** with unbonding periods, stake stays slashable while it could still sign old forks.\n\n"
     "Net effect: the security budget becomes \"value at risk\" instead of \"energy spent\"."),
    (("sky", "blue"),
     "The sky looks blue because of Rayleigh scattering: air molecules scatter light with intensity "
     "proportional to 1/λ⁴, so short (blue) wavelengths scatter roughly 5× more than red. We see that "
     "scattered blue light from every direction. It isn't violet because sunlight has less violet, some is "
     "absorbed high in the atmosphere, and our eyes are less sensitive to it. At sunset light travels through "
     "more air, the blue is scattered out, and reds and oranges remain."),
    (("summarize", "log"),
     "Summary: the log is a long run of repetitive, routine server output with no errors, warnings or "
     "stack traces. Nothing needs action; consider lowering the log level to cut the noise."),
]


def _answer(model: str, prompt: str) -> str:
    low = prompt.lower()
    for keys, text in CANNED:
        if all(k in low for k in keys):
            return text
    topic = " ".join(prompt.split())[:80]
    return {
        "tiny": f"Short answer: {topic} depends on the context; can you give a bit more detail?",
        "mid": f"Here's an overview of \"{topic}\": the key points are the inputs, the constraints, and the "
               "expected result. Share specifics and I can be more precise.",
        "big": f"Let's work through \"{topic}\" step by step: first clarify the goal, then the constraints, "
               "then compare options and pick the one with the best tradeoff.",
    }[model]


@app.get("/v1/health")
def health() -> dict:
    return {"ok": True}


@app.post("/v1/{model}")
def generate(model: str, req: GenerateRequest) -> dict:
    if model not in ("tiny", "mid", "big"):
        raise HTTPException(status_code=404, detail=f"unknown model {model!r}")
    if f"[fail:{model}]" in req.prompt:
        log.warning("mock-%s returning empty answer (fault-injection marker)", model)
        return {"output": {"text": ""}, "model": f"mock-{model}"}
    return {"output": {"text": _answer(model, req.prompt)}, "model": f"mock-{model}"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8101)
