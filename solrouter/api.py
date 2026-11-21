"""SolRouter HTTP API. Run: uvicorn api:app --port 8010"""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from service import MAX_PROMPT_CHARS, RequestRejected, SolRouter

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="SolRouter")
router = SolRouter()


class AskIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    max_price_usd: float | None = Field(default=None, gt=0, le=10)
    min_tier: str | None = Field(default=None, pattern="^(cheap|mid|premium)$")


@app.post("/ask")
def ask(body: AskIn) -> dict:
    try:
        r = router.ask(body.prompt, body.max_price_usd, body.min_tier)
    except RequestRejected as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return asdict(r)


@app.get("/stats")
def stats() -> dict:
    return router.ledger.totals()


@app.get("/models")
def models() -> list[dict]:
    return [asdict(m) for m in router.models]
