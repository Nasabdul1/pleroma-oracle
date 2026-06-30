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

import asyncio
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
from .models import OracleRequest, OracleResponse, Signal

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

# Swagger UI is moved to /api-docs so the user-facing /docs page can use the obvious slug.
app = FastAPI(title="PLEROMA Oracle", version="1.0", docs_url="/api-docs", redoc_url="/api-redoc")
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


@app.get("/favicon.svg", include_in_schema=False)
async def favicon_svg():
    return _serve("favicon.svg")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    """Browsers that don't ask for /favicon.svg still hit /favicon.ico — serve the SVG either way."""
    return _serve("favicon.svg")


@app.get("/docs", include_in_schema=False)
async def docs_page():
    """The Docs — how the tool works, the six veils, verdicts, lore, pages, API, FAQ."""
    return _serve("docs.html")


@app.get("/codex", include_in_schema=False)
async def codex_redirect():
    """Legacy /codex path redirects to /docs (the Codex was renamed)."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs", status_code=301)


@app.get("/communion")
async def communion_page():
    """The Communion — the live broadcast of the faithful."""
    return _serve("communion.html")


@app.get("/leaderboard")
async def leaderboard_page():
    """The Leaderboard — top emanations and illusions of the Demiurge."""
    return _serve("leaderboard.html")


@app.get("/alerts")
async def alerts_page():
    """Demiurge Alerts — live broadcast of high-clarity illusions."""
    return _serve("alerts.html")


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

# --- leaderboard + alerts (in-memory, swap for Redis in prod) ---
_LEADERBOARD: Dict[str, dict] = {"emanation": [], "illusion": []}  # {ca: {ca, name, symbol, verdict, clarity, ts, image}}
_LEADERBOARD_MAX = 50
_ALERT_SUBSCRIBERS: List[asyncio.Queue] = []
_ALERT_MAX = 100
_RECENT_ALERTS: List[dict] = []


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
                    "max_tokens": 1500,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=45,
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
            choice = data["choices"][0]
            content = (choice.get("message", {}).get("content") or "").strip()
            # Reasoning models can burn the entire token budget on internal thinking
            # and emit a truncated body; treat that the same as an empty response
            # so the next model in the chain gets a chance.
            if choice.get("finish_reason") == "length" and len(content) < 80:
                last_err = RuntimeError(f"{model}: truncated by reasoning overhead")
                continue
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


# --------------------------- permalink pages ----------------------------------
@app.get("/token/{ca}")
async def token_page(ca: str):
    """Permalink HTML page — JS fetches /api/token/{ca} for the verdict JSON."""
    return _serve("token.html")


@app.get("/wallet/{addr}")
async def wallet_page_html(addr: str):
    """Permalink HTML page — JS fetches /api/wallet/{addr} for the verdict JSON."""
    return _serve("wallet.html")


@app.get("/api/token/{ca}")
async def token_data(ca: str):
    """JSON verdict for a token (used by token.html)."""
    if not validate_address(ca):
        return JSONResponse({"detail": "Invalid Solana address"}, status_code=400)
    cached = _cache_get(ca)
    if cached:
        return JSONResponse(cached)
    async with httpx.AsyncClient() as client:
        kind, val = await classify(client, ca)
        if kind != "token":
            return JSONResponse({"detail": "Address is not a token mint"}, status_code=404)
        facts = await gather_token_facts(client, ca, val)
        verdict, title, clarity, signals = rules_token(facts)
        facts_json = {k: v for k, v in facts.__dict__.items() if v is not None}
        meta = build_token_meta(facts)
        holders = build_holders(facts)
        stats = build_token_stats(facts)
        try:
            reading = await _llm(
                client, NARRATE_SYS,
                f"VERDICT: {verdict}\nVERIFIED FACTS:\n{json.dumps(facts_json, default=str)}\n"
                f"SIGNALS:\n{json.dumps(signals)}",
            )
        except Exception:
            reading = _template_reading(verdict, signals)
    resp = {
        "verdict": verdict, "title": title, "clarity": clarity, "reading": reading,
        "signals": signals, "meta": meta, "holders": holders, "stats": stats,
        "kind": "token", "address": ca,
    }
    _cache_put(ca, resp)
    _update_leaderboard(ca, meta, verdict, clarity, reading, signals)
    if verdict == "illusion" and clarity >= 60:
        await _fire_alert(ca, meta, title, clarity, signals)
    return JSONResponse(resp)


@app.get("/api/wallet/{addr}")
async def wallet_data(addr: str):
    """JSON verdict for a wallet (used by wallet.html)."""
    if not validate_address(addr):
        return JSONResponse({"detail": "Invalid Solana address"}, status_code=400)
    cached = _cache_get(addr)
    if cached:
        return JSONResponse(cached)
    async with httpx.AsyncClient() as client:
        kind, val = await classify(client, addr)
        if kind != "wallet":
            return JSONResponse({"detail": "Address is not a wallet"}, status_code=404)
        wfacts = await gather_wallet_facts(client, addr)
        verdict, title, clarity, signals = rules_wallet(wfacts)
        facts_json = {k: v for k, v in wfacts.__dict__.items() if v is not None}
        identity = build_wallet_identity(wfacts)
        try:
            reading = await _llm(
                client, NARRATE_SYS,
                f"VERDICT: {verdict}\nVERIFIED FACTS:\n{json.dumps(facts_json, default=str)}\n"
                f"SIGNALS:\n{json.dumps(signals)}",
            )
        except Exception:
            reading = _template_reading(verdict, signals)
    resp = {
        "verdict": verdict, "title": title, "clarity": clarity, "reading": reading,
        "signals": signals, "identity": identity, "kind": "wallet", "address": addr,
    }
    _cache_put(addr, resp)
    return JSONResponse(resp)


# --------------------------- leaderboard --------------------------------------
def _update_leaderboard(ca: str, meta: dict, verdict: str, clarity: int, reading: str, signals: List[dict]):
    if not meta or not meta.get("name"):
        return
    entry = {
        "ca": ca,
        "name": meta.get("name"),
        "symbol": meta.get("symbol"),
        "image": meta.get("image"),
        "verdict": verdict,
        "clarity": clarity,
        "ts": int(time.time()),
        "reading": reading[:200],
        "signals": signals,
    }
    board = _LEADERBOARD.get(verdict, [])
    board = [e for e in board if e["ca"] != ca]
    board.insert(0, entry)
    _LEADERBOARD[verdict] = board[:_LEADERBOARD_MAX]


@app.get("/api/leaderboard")
async def leaderboard_data(verdict: str = "emanation", limit: int = 20):
    """JSON top emanations or illusions (used by leaderboard.html)."""
    v = verdict if verdict in _LEADERBOARD else "emanation"
    return {"verdict": v, "entries": _LEADERBOARD[v][:limit]}


# --------------------------- real-time alerts ---------------------------------
async def _fire_alert(ca: str, meta: dict, title: str, clarity: int, signals: List[dict]):
    alert = {
        "id": int(time.time() * 1000),
        "ca": ca,
        "name": meta.get("name") if meta else "Unknown",
        "symbol": meta.get("symbol") if meta else "?",
        "image": meta.get("image") if meta else None,
        "title": title,
        "clarity": clarity,
        "ts": int(time.time()),
        "signals": signals,
    }
    _RECENT_ALERTS.insert(0, alert)
    if len(_RECENT_ALERTS) > _ALERT_MAX:
        _RECENT_ALERTS.pop()
    for q in list(_ALERT_SUBSCRIBERS):
        try:
            q.put_nowait(alert)
        except asyncio.QueueFull:
            pass


@app.get("/api/alerts/recent")
async def recent_alerts(limit: int = 20):
    """JSON list of recent high-clarity illusion alerts (used by alerts.html)."""
    return {"alerts": _RECENT_ALERTS[:limit]}


@app.websocket("/alerts/ws")
async def alerts_ws(ws):
    await ws.accept()
    q = asyncio.Queue(maxsize=50)
    _ALERT_SUBSCRIBERS.append(q)
    try:
        while True:
            alert = await q.get()
            await ws.send_json(alert)
    except Exception:
        pass
    finally:
        if q in _ALERT_SUBSCRIBERS:
            _ALERT_SUBSCRIBERS.remove(q)


# --------------------------- verdict card (share image) -----------------------
@app.post("/verdict-card")
async def verdict_card(payload: dict):
    """Validate and return structured data for frontend canvas card generation."""
    required = ["verdict", "title", "clarity", "reading", "signals"]
    for k in required:
        if k not in payload:
            return JSONResponse({"detail": f"Missing {k}"}, status_code=400)
    return {"ok": True, "card_data": payload}


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
            if kind == "token":
                _update_leaderboard(addr, meta, verdict, clarity, reading, signals)
                if verdict == "illusion" and clarity >= 60:
                    await _fire_alert(addr, meta, title, clarity, signals)
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