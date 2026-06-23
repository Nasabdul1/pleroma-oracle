"""Request/response schemas. The OracleResponse shape matches what pleroma.html's
render() expects, plus optional display extras (meta/holders/identity)."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

Verdict = Literal["emanation", "illusion", "veiled"]
State = Literal["light", "shadow", "unknown"]
Icon = Literal["holders", "liquidity", "deployer", "hype", "origin", "snare", "veil"]


class OracleRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)


class Signal(BaseModel):
    name: str
    icon: Icon
    state: State
    note: str


class OracleResponse(BaseModel):
    verdict: Verdict
    title: str
    clarity: int = Field(..., ge=0, le=100)
    reading: str
    signals: List[Signal]
    # --- optional display extras (rendered as boxes when present) ---
    meta: Optional[Dict[str, Any]] = None       # token lore + socials
    holders: Optional[List[Dict[str, Any]]] = None   # top-10 holders
    identity: Optional[Dict[str, Any]] = None   # wallet .sol domains etc.
    # echoed metadata
    kind: Optional[str] = None
    address: Optional[str] = None