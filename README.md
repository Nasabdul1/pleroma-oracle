# PLEROMA Oracle

A crypto truth oracle for Solana. Paste a token mint or wallet — or ask a
question in plain words — and get one verdict (**Emanation / Illusion / Veiled**),
a clarity score, a 2–3 sentence reading, and the verified signals behind it.

The backend pulls **verified on-chain facts** in parallel (RPC + RugCheck +
DexScreener), runs a **deterministic rules engine** to decide the verdict, and
the LLM only writes the prose. The model can never flip a verdict, so it can't
hallucinate a token "safe."

---

## How the site works

### One backend, one origin

FastAPI (`app/main.py`) serves both the HTML pages and the JSON API from the
same origin. There's no separate frontend build — pages are static `.html`
files at the repo root, and the backend serves them by route:

| URL | Serves | What it does |
| --- | --- | --- |
| `/` | `pleroma.html` | The Oracle — the homepage console |
| `/docs` | `docs.html` | Full manual — pipeline, veils, verdicts, pages, lore, API, FAQ (also reachable at `/codex` via 301 redirect) |
| `/communion` | `communion.html` | Live broadcast chat between users |
| `/leaderboard` | `leaderboard.html` | Top emanations & illusions board |
| `/alerts` | `alerts.html` | Real-time Demiurge alerts feed |
| `/token/{ca}` | `token.html` | Shareable permalink for a token verdict |
| `/wallet/{addr}` | `wallet.html` | Shareable permalink for a wallet reading |

Each HTML page is self-contained (inline CSS, inline SVG symbols, vanilla JS —
no bundler, no framework) and calls the JSON API for its data.

### The Oracle flow (the homepage)

1. User types a Solana address or a question into the console on `/`.
2. `POST /oracle { query }` hits the backend.
3. Backend extracts an address from the query if present.
4. **If an address**:
   - `classify()` decides token-mint vs wallet vs not-found.
   - For a token: `gather_token_facts()` fans out to RPC + RugCheck + DexScreener
     in parallel. For a wallet: `gather_wallet_facts()` does the same with
     Jupiter pricing and `.sol` domain lookups.
   - `rules_token()` / `rules_wallet()` — deterministic — fixes the verdict,
     title, clarity score, and the signals list.
   - The LLM is then asked to write **only** the prose `reading` from those
     verified facts. If the model chain fails, a templated reading is used —
     the verdict still ships.
5. **If no address** (a question): the LLM answers in PLEROMA's voice and
   returns the full JSON shape directly.
6. The browser renders the verdict card with signals, the contract box, lore,
   holders, market tiles, identity, etc. — whichever sections the response
   includes.

### The verdict engine

Verdict logic lives in `app/analysis.py`. The rules engine maps verified facts
to one of three verdicts:

- **Emanation of the Fullness** — no structural deceptions surface; checks
  return clean.
- **Illusion of the Demiurge** — concrete red flags (unrenounced mint, unlocked
  LP, top-1 concentration, freeze authority, honeypot markers, etc.).
- **Veiled in Shadow** — too little verified data to render a true judgment.

The clarity score is the engine's confidence: more verified facts and clearer
signals push it up. Every signal carries an icon, a state (`light`/`shadow`/
`unknown`), and a one-line note tying it back to a verified fact.

Why model-narrates-only: even when an LLM goes sideways, the structural verdict
stays correct. The test suite (`tests/test_analysis.py`) is the safety net —
parametrize fixtures cover bluechip, clean launches, rugs, honeypots, sparse
data, and freeze authority. If a verdict ever flips, the build fails before
users see it.

### Permalinks (`/token/{ca}` and `/wallet/{addr}`)

When a user gets a verdict on the homepage, the action strip shows "Open
Permalink" — that link points at `/token/{ca}` (or `/wallet/{addr}`), which
serves a standalone HTML page. The page reads the address from its own URL
path, fetches `/api/token/{ca}` (or `/api/wallet/{addr}`) for the JSON, and
renders a full report:

- Token page: hero verdict + clarity, market tiles, embedded DexScreener chart,
  top-10 holders bar chart, lore + socials, contract box, share-to-X.
- Wallet page: hero verdict, total worth, SOL balance, top holdings grid,
  scanned token images, `.sol` identity chips, signals.

Both pages set `og:` meta tags from the verdict so X/Discord previews render
the title and reading.

### Leaderboard

Every successful token verdict is automatically pushed onto `_LEADERBOARD`
(in-memory) — keyed by verdict (`emanation` or `illusion`), newest-first, max
50 per side, deduped by CA. `leaderboard.html` is a tabbed board that polls
`/api/leaderboard?verdict=…` every 30s. Fresh entries between polls get a
"NEW" badge so the board feels alive.

### Demiurge Alerts (real-time)

When `rules_token()` returns `verdict == "illusion"` **and** `clarity >= 60`,
`_fire_alert()` runs:

- The alert (token, image, title, clarity, top signals, ts) is prepended to
  `_RECENT_ALERTS` (cap 100).
- It's also pushed to every connected WebSocket subscriber on `/alerts/ws`.

`alerts.html` does two things on load:

1. `GET /api/alerts/recent` — fills the feed with the most recent N illusions.
2. Opens `WebSocket(/alerts/ws)` — every new illusion arrives live, gets
   prepended to the feed with a flash animation, fires a desktop notification
   (if granted), and optionally a synthesized two-tone siren beep.

The WebSocket auto-reconnects with exponential backoff if the channel drops.

### The Communion (live chat)

`communion.html` polls `GET /communion/messages?after=<last_id>` for new
messages and posts via `POST /communion/post {name, text}`. Server-side it's
an in-memory ring buffer (cap 300) with IP-based rate limiting. Messages do
not persist across restarts — swap `_COMMUNION` for Redis or Firebase for that.

### The PLEROMA token banner

The sticky bar under the nav on `/` is independent of the scanner. It shows
the project's own token: `GET /token/stats` reads `PLEROMA_CA` from env, hits
DexScreener once per cache window, and returns market cap + 24h change + a
"Live / Pre-launch / Live soon" tag. Pre-launch (empty `PLEROMA_CA`) renders
"Revealed at launch" and hides the copy button. Refresh interval is 30s.

### Caching & rate limiting

- `_cache` — 45s TTL keyed by address or `q:<lowercased question>`. Multiple
  users asking about the same token in the same minute hit RPC/RugCheck once.
- `_hits` — per-IP sliding window, 30 req/min default. On limit hit, the
  Oracle returns a `veiled` verdict in-voice (never a raw 429 error).
- Both are in-memory — swap for Redis to shard across replicas.

### The LLM (OpenRouter)

`OPENROUTER_MODELS` is a comma-separated **fallback chain** (env var).
`_llm()` tries each model in order; if one is dead (404 "No endpoints found"),
throttled (429), or returns a body with no choices, it falls through to the
next. Default chain:

```
meta-llama/llama-3.3-70b-instruct:free,
deepseek/deepseek-chat-v3-0324:free,
google/gemini-2.0-flash-exp:free
```

If the whole chain fails, the templated reading still ships — the verdict
never goes down because a model went down.

---

## API surface

HTML pages (above) plus:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness + whether `OPENROUTER_API_KEY` is set |
| POST | `/oracle` | Main verdict endpoint (`{query}` → full verdict JSON) |
| GET | `/token/stats` | Live market data for the PLEROMA token banner |
| GET | `/api/token/{ca}` | JSON verdict for a token (used by `token.html`) |
| GET | `/api/wallet/{addr}` | JSON verdict for a wallet (used by `wallet.html`) |
| GET | `/api/leaderboard?verdict=&limit=` | Leaderboard JSON (used by `leaderboard.html`) |
| GET | `/api/alerts/recent?limit=` | Recent illusions JSON (used by `alerts.html`) |
| WS | `/alerts/ws` | Live illusion broadcast (used by `alerts.html`) |
| GET | `/communion/messages?after=` | Poll for new chat messages |
| POST | `/communion/post` | Post a chat message |
| POST | `/verdict-card` | Validate verdict shape for canvas card generation |

---

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate    # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env          # add OPENROUTER_API_KEY + (recommended) a Helius RPC URL
uvicorn app.main:app --reload --port 8000
# Site:  http://localhost:8000
# Docs:  http://localhost:8000/docs
```

## Test

```bash
pytest -q          # rules-engine + validation tests, no network
pytest -m live     # opt-in smoke tests against real addresses (USDC, etc.)
```

The `parametrize` table in `tests/test_analysis.py` is the labeled fixture
set — clean token, bluechip, mint-authority rug, unlocked-LP rug, >50%
concentration, freeze-authority, sparse data, low liquidity. Add post-mortem
rug addresses and known honeypots as new fixtures; if a verdict ever flips,
the build fails before users see it.

## Deploy (Railway)

```
# Procfile
web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Set the env vars from `.env.example` in the Railway dashboard. Set
`ALLOWED_ORIGINS` to your site's domain (not `*`) in production.

Env vars worth tuning:

- `OPENROUTER_API_KEY` — required for prose readings (verdicts work without it).
- `OPENROUTER_MODELS` — comma-separated fallback chain.
- `PLEROMA_CA` — your token contract; empty until launch.
- `CACHE_TTL` (default 45s), `RATE_PER_MIN` (default 30).
- `ALLOWED_ORIGINS` — CORS allowlist; comma-separated.

---

## Accuracy notes / what to verify before launch

- **RugCheck endpoint & field names** — `_rugcheck()` parses defensively (a
  shape change degrades to "unverified," never a crash), but confirm the live
  response shape at https://api.rugcheck.xyz and adjust `lpLockedPct` / score
  keys.
- **Holder concentration** is computed from `getTokenLargestAccounts` (token-
  account level). For owner-level accuracy, prefer RugCheck's `topHolders` or
  a Helius DAS query and set `top1_pct` from that.
- **Honeypot / sell-tax** currently uses the Token-2022 transfer-fee extension.
  For classic SPL honeypots, add a Jupiter quote both directions and flag if a
  sell route doesn't exist or carries an abnormal tax.
- **Wallet deployer history** is shallow without an indexer — wire Helius DAS /
  enhanced transactions to fill "The Hidden Hand."
- **Use a real RPC** (Helius/Triton). Public `mainnet-beta` will rate-limit
  you.
- **In-memory state** (`_cache`, `_hits`, `_LEADERBOARD`, `_RECENT_ALERTS`,
  `_COMMUNION`, `_ALERT_SUBSCRIBERS`) resets on restart and is **not** shared
  across multiple replicas. Back with Redis for prod scale.
- **The ceiling stays honest**: a clean result means "no structural red flags,"
  not "safe to buy." Keep that in the UI disclaimer.

---

## Layout

```
pleroma-oracle/
  app/
    main.py        FastAPI: routes, cache, rate-limit, alerts, leaderboard, LLM
    analysis.py    validation, classification, fetchers, rules engine  <- the brain
    models.py      pydantic schemas (response = what the pages render)
  pleroma.html     The Oracle (homepage)
  docs.html        Full manual: pipeline, veils, verdicts, pages, lore, API, FAQ
  communion.html   Live broadcast chat
  leaderboard.html Top emanations & illusions board
  alerts.html      Real-time Demiurge alerts feed
  token.html       Token verdict permalink
  wallet.html      Wallet reading permalink
  tests/test_analysis.py
  requirements.txt  Procfile  pytest.ini
```
