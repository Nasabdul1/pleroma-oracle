"""
PLEROMA Oracle backend (OpenRouter edition).

POST /oracle  { "query": "<solana address OR a question>" }
  -> { verdict, title, clarity, reading, signals[] }   (exactly what pleroma.html renders)

Flow:
  - address present  -> classify -> gather verified facts -> deterministic rules engine
                        -> model writes ONLY the prose `reading` from those facts
  - no address       -> conceptual question -> model answers in PLEROMA's voice (same shape)

The model never sets the verdict, clarity, or signals for an address. It narrates.
If the model is unavailable, a templated reading is used so the API still works.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from typing import Dict, List, Tuple

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pathlib import Path

from .analysis import (
    extract_address, validate_address, classify,
    gather_token_facts, gather_wallet_facts,
    rules_token, rules_wallet, TokenFacts, WalletFacts,
    build_token_meta, build_holders, build_wallet_identity, build_token_stats,
)
from .models import OracleRequest, OracleResponse

load_dotenv()

# --- model (OpenRouter). The LLM ONLY writes the prose "reading". ---
# OPENROUTER_MODELS is a comma-separated FALLBACK CHAIN. If the first model is
# unavailable (404 "No endpoints found" — common for :free experimental models)
# or rate-limited (429), the next one is tried automatically. This is why the
# old single google/gemini-2.0-flash-exp:free pin took the whole oracle down.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
_models_raw = (
    os.getenv("OPENROUTER_MODELS")
    or os.getenv("OPENROUTER_MODEL")
    or "meta-llama/llama-3.3-70b-instruct:free,"
       "deepseek/deepseek-chat-v3-0324:free,"
       "google/gemini-2.0-flash-exp:free"
)
OPENROUTER_MODELS = [m.strip() for m in _models_raw.split(",") if m.strip()]
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_REFERER = os.getenv("OPENROUTER_REFERER", "https://pleroma.app")

# --- service ---
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
CACHE_TTL = float(os.getenv("CACHE_TTL", "45"))
RATE_PER_MIN = int(os.getenv("RATE_PER_MIN", "30"))
# The PLEROMA token contract. Leave empty until launch; swap in .env to go live.
PLEROMA_CA = os.getenv("PLEROMA_CA", "").strip()

app = FastAPI(title="PLEROMA Oracle", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# project root = one level above app/ ; pleroma.html lives there
BASE_DIR = Path(__file__).resolve().parent.parent


def _serve(filename: str):
    f = BASE_DIR / filename
    if f.exists():
        return FileResponse(f)
    return JSONResponse(
        {"detail": filename + " not found - place it in the project root (next to the app/ folder)."},
        status_code=404,
    )


@app.get("/")
async def home():
    """The Oracle (consult) page."""
    return _serve("pleroma.html")


@app.get("/codex")
async def codex():
    """The Codex — doctrine, veils, verdicts, token, FAQ."""
    return _serve("codex.html")


@app.get("/communion")
async def communion_page():
    """The Communion — the live broadcast of the faithful."""
    return _serve("communion.html")


# --------------------------- The Communion (live chat) -----------------------
# In-memory broadcast. Messages clear on restart and are NOT shared across
# multiple server instances. For production persistence, back this with Redis
# or Firebase (see README).
_COMMUNION: List[dict] = []
_COMMUNION_SEQ = {"n": 0}
_COMMUNION_MAX = 300


@app.get("/communion/messages")
async def communion_messages(after: int = 0):
    msgs = [m for m in _COMMUNION if m["id"] > after]
    last = _COMMUNION[-1]["id"] if _COMMUNION else 0
    return {"messages": msgs[-100:], "last_id": last, "online": len(_hits)}


@app.post("/communion/post")
async def communion_post(payload: dict, request: Request):
    ip = request.client.host if request.client else "anon"
    if not _rate_ok("communion:" + ip):
        return JSONResponse(status_code=429, content={"ok": False, "detail": "Slow down, faithful one."})
    name = str(payload.get("name", "")).strip()[:24] or "Anonymous Aeon"
    text = str(payload.get("text", "")).strip()[:500]
    if not text:
        return JSONResponse(status_code=400, content={"ok": False, "detail": "Empty utterance."})
    _COMMUNION_SEQ["n"] += 1
    msg = {"id": _COMMUNION_SEQ["n"], "name": name, "text": text, "ts": int(time.time())}
    _COMMUNION.append(msg)
    if len(_COMMUNION) > _COMMUNION_MAX:
        del _COMMUNION[: len(_COMMUNION) - _COMMUNION_MAX]
    return {"ok": True, "message": msg}

# ------ tiny in-memory cache + rate limiter (swap for Redis in prod) ----------
_cache: Dict[str, Tuple[float, dict]] = {}
_hits: Dict[str, List[float]] = defaultdict(list)


def _cache_get(key: str):
    v = _cache.get(key)
    if v and time.time() - v[0] < CACHE_TTL:
        return v[1]
    return None


def _cache_put(key: str, val: dict):
    _cache[key] = (time.time(), val)


def _rate_ok(ip: str) -> bool:
    now = time.time()
    _hits[ip] = [t for t in _hits[ip] if now - t < 60]
    if len(_hits[ip]) >= RATE_PER_MIN:
        return False
    _hits[ip].append(now)
    return True


# --------------------------- model narration ---------------------------------

NARRATE_SYS = (
    "You are PLEROMA, a crypto truth oracle in the Gnostic voice (the Fullness vs the "
    "Demiurge's illusions; light vs shadow). You are given a VERDICT and a list of "
    "VERIFIED on-chain facts. Write ONLY a 2-3 sentence 'reading': plain, genuinely useful, "
    "calm and a touch sacred. Rules: narrate only the facts given; never invent a number or "
    "claim; if facts are thin, say the picture is incomplete. Output the reading text only — "
    "no JSON, no preamble, no markdown."
)

QUESTION_SYS = (
    "You are PLEROMA, a crypto truth oracle (Gnostic voice: the Fullness vs the Demiurge's "
    "illusions). Answer the user's crypto-safety question genuinely and expertly, plainly and "
    "usefully, with a touch of the sacred. Return ONLY JSON, no markdown:\n"
    '{"verdict":"emanation|illusion|veiled","title":"SHORT ALL-CAPS PHRASE","clarity":0-100,'
    '"reading":"2-4 plain sentences","signals":[{"name":"label","icon":'
    '"holders|liquidity|deployer|hype|origin|snare|veil","state":"light|shadow|unknown",'
    '"note":"one line"}]}'
)


async def _llm(client: httpx.AsyncClient, system: str, user: str) -> str:
    """Try each model in the fallback chain until one answers.

    OpenRouter signals a dead/invalid model with HTTP 404 ("No endpoints found")
    and a busy free model with 429 — we skip both and try the next. It can also
    return HTTP 200 with an {"error": ...} body and no choices, which we also skip.
    """
    if not OPENROUTER_API_KEY:
        raise RuntimeError("no api key")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_REFERER,
        "X-Title": "PLEROMA Oracle",
    }
    last_err: Exception | None = None
    for model in OPENROUTER_MODELS:
        try:
            r = await client.post(
                OPENROUTER_URL,
                headers=headers,
                json={
                    "model": model,
                    "max_tokens": 700,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=30,
            )
            # model unavailable (404) or throttled (429) -> next model in the chain
            if r.status_code in (404, 429, 502, 503):
                last_err = RuntimeError(f"{model}: HTTP {r.status_code}")
                continue
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("error") and not data.get("choices"):
                last_err = RuntimeError(f"{model}: {data['error']}")
                continue
            content = (data["choices"][0]["message"]["content"] or "").strip()
            if content:
                return content
            last_err = RuntimeError(f"{model}: empty response")
        except Exception as e:  # network / parse / shape — try the next model
            last_err = e
            continue
    raise last_err or RuntimeError("all models in the chain failed")


def _template_reading(verdict: str, signals: List[dict]) -> str:
    """Fallback when the model is unavailable — built only from verified signals."""
    shadows = [s["note"] for s in signals if s["state"] == "shadow"]
    if verdict == "illusion":
        return "The Demiurge's hand is on this one. " + " ".join(shadows[:2])
    if verdict == "emanation":
        return ("No structural deceptions surface here — the checks return clean. "
                "Verify the team and socials yourself before you commit.")
    return ("The picture is incomplete; too little could be verified to render a true "
            "judgment. Treat with caution and seek more light.")


# --------------------------------- route -------------------------------------

@app.get("/health")
async def health():
    return {"ok": True, "model_bound": bool(OPENROUTER_API_KEY)}


@app.get("/token/stats")
async def token_stats():
    """Live market data for the PLEROMA token ONLY (server-configured CA).
    Independent of the scanner — never reflects a scanned address."""
    cached = _cache_get("__pleroma_token__")
    if cached:
        return cached
    if not PLEROMA_CA:
        out = {"launched": False, "symbol": "PLEROMA"}
        _cache_put("__pleroma_token__", out)
        return out
    out = {"launched": True, "ca": PLEROMA_CA, "symbol": "PLEROMA"}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{PLEROMA_CA}", timeout=8)
            pairs = r.json().get("pairs") or []
        if not pairs:
            out["pending"] = True
        else:
            best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
            bt = best.get("baseToken") or {}
            out.update({
                "name": bt.get("name"),
                "symbol": bt.get("symbol") or "PLEROMA",
                "price": best.get("priceUsd"),
                "mcap": best.get("marketCap") or best.get("fdv"),
                "liquidity": (best.get("liquidity") or {}).get("usd"),
                "change24h": (best.get("priceChange") or {}).get("h24"),
                "image": (best.get("info") or {}).get("imageUrl"),
                "url": best.get("url"),
            })
    except Exception:
        out["error"] = True
    _cache_put("__pleroma_token__", out)
    return out


@app.post("/oracle", response_model=OracleResponse)
async def oracle(req: OracleRequest, request: Request):
    ip = request.client.host if request.client else "anon"
    if not _rate_ok(ip):
        return JSONResponse(status_code=429, content={
            "verdict": "veiled", "title": "THE VEIL HOLDS", "clarity": 0,
            "reading": "Too many inquiries too quickly. Let the channel rest a moment.",
            "signals": [{"name": "The Channel", "icon": "veil", "state": "unknown",
                         "note": "Rate limit reached."}],
        })

    query = req.query.strip()
    addr = extract_address(query)

    cache_key = addr or ("q:" + query.lower())
    if (cached := _cache_get(cache_key)):
        return cached

    async with httpx.AsyncClient() as client:
        # ---- path A: a Solana address ----
        if addr:
            kind, val = await classify(client, addr)
            meta = holders = identity = stats = None
            if kind == "token":
                facts = await gather_token_facts(client, addr, val)
                verdict, title, clarity, signals = rules_token(facts)
                facts_json = {k: v for k, v in facts.__dict__.items() if v is not None}
                meta = build_token_meta(facts)
                holders = build_holders(facts)
                stats = build_token_stats(facts)
            elif kind == "wallet":
                wfacts = await gather_wallet_facts(client, addr)
                verdict, title, clarity, signals = rules_wallet(wfacts)
                facts_json = {k: v for k, v in wfacts.__dict__.items() if v is not None}
                identity = build_wallet_identity(wfacts)
            else:
                resp = OracleResponse(
                    verdict="veiled", title="VEILED IN SHADOW", clarity=10,
                    reading="This is a valid address, but nothing is written of it on-chain. There is nothing here to read.",
                    signals=[{"name": "The Origin", "icon": "veil", "state": "unknown",
                              "note": "No account found for this address."}],
                    kind="notfound", address=addr,
                ).model_dump()
                _cache_put(cache_key, resp)
                return resp

            # model narrates ONLY (verdict already fixed)
            try:
                reading = await _llm(
                    client, NARRATE_SYS,
                    f"VERDICT: {verdict}\nVERIFIED FACTS:\n{json.dumps(facts_json, default=str)}\n"
                    f"SIGNALS:\n{json.dumps(signals)}",
                )
                if not reading:
                    raise RuntimeError("empty")
            except Exception:
                reading = _template_reading(verdict, signals)

            resp = OracleResponse(
                verdict=verdict, title=title, clarity=clarity, reading=reading,
                signals=signals, meta=meta, holders=holders, identity=identity,
                stats=stats, kind=kind, address=addr,
            ).model_dump()
            _cache_put(cache_key, resp)
            return resp

        # ---- path B: a conceptual question (no address) ----
        try:
            raw = await _llm(client, QUESTION_SYS, query)
            raw = raw.replace("```json", "").replace("```", "").strip()
            data = json.loads(raw)
            resp = OracleResponse(**{**data, "kind": "question"}).model_dump()
        except Exception:
            # Whole model chain unavailable. Don't leak the upstream URL/stack —
            # answer in-voice so the buttons never show a broken card.
            resp = OracleResponse(
                verdict="veiled", title="THE VEIL HOLDS", clarity=0,
                reading=("The channel to the Fullness is clouded for a moment — the "
                         "Oracle cannot speak in prose right now. Bring a token or wallet "
                         "address and it will still render a precise on-chain judgment, "
                         "or ask the question again shortly."),
                signals=[{"name": "The Channel", "icon": "veil", "state": "unknown",
                          "note": "The reasoning channel is resting. On-chain reading is unaffected."}],
                kind="question",
            ).model_dump()
        _cache_put(cache_key, resp)
        return resp