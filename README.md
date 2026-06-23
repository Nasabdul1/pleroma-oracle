# PLEROMA Oracle — backend

Validates a Solana address, classifies it (token vs wallet), pulls **verified**
on-chain facts from RPC + RugCheck + DexScreener in parallel, runs a
**deterministic rules engine**, and returns the exact JSON your `pleroma.html`
already renders. The model only writes the prose — it never decides the verdict,
so it can't hallucinate a token "safe."

```
POST /oracle   { "query": "<solana address OR a question>" }
->  { verdict, title, clarity, reading, signals:[{name,icon,state,note}] }
GET  /health
```

## Run locally

```bash
cd oracle
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add ANTHROPIC_API_KEY + (recommended) a Helius RPC URL
uvicorn app.main:app --reload --port 8000
# docs at http://localhost:8000/docs
```

## Test (proves accuracy on every deploy)

```bash
pytest -q          # 16 rules-engine + validation tests, no network
pytest -m live     # opt-in smoke tests against real addresses (USDC, etc.)
```

The `parametrize` table in `tests/test_analysis.py` is your labeled fixture set —
clean token, bluechip, mint-authority rug, unlocked-LP rug, >50% concentration,
freeze-authority, sparse data, low liquidity. Add post-mortem rug addresses and
known honeypots as new fixtures; if a verdict ever flips, the build fails before
users see it.

## Point the site at it

In `pleroma.html`, replace the `fetch('https://api.anthropic.com/...')` block
inside `consult()` with:

```js
const res = await fetch(BACKEND_URL + '/oracle', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ query: q })
});
const r = await res.json();
render(r);            // render() already expects {verdict,title,clarity,reading,signals}
```

Set `const BACKEND_URL = 'https://your-oracle.up.railway.app';` near the top of the
script. Delete the old `SYSTEM` prompt and direct-Anthropic call — that logic now
lives server-side, and your API key never touches the browser.

## Deploy (Railway)

```bash
# Procfile
web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```
Set the env vars from `.env.example` in the Railway dashboard. Set
`ALLOWED_ORIGINS` to your site's domain (not `*`) in production.

## Accuracy notes / what to verify before launch

- **RugCheck endpoint & field names** — `_rugcheck()` parses defensively (a shape
  change degrades to "unverified," never a crash), but confirm the live response
  shape at https://api.rugcheck.xyz and adjust `lpLockedPct` / score keys.
- **Holder concentration** is computed from `getTokenLargestAccounts` (token-account
  level). For owner-level accuracy, prefer RugCheck's `topHolders` or a Helius DAS
  query and set `top1_pct` from that.
- **Honeypot / sell-tax** currently uses the Token-2022 transfer-fee extension.
  For classic SPL honeypots, add a Jupiter quote both directions and flag if a sell
  route doesn't exist or carries an abnormal tax.
- **Wallet deployer history** is shallow without an indexer — wire Helius DAS /
  enhanced transactions to fill "The Hidden Hand."
- **Use a real RPC** (Helius/Triton). Public mainnet-beta will rate-limit you.
- **The ceiling stays honest**: a clean result means "no structural red flags,"
  not "safe to buy." Keep that in the UI disclaimer.

## Layout

```
oracle/
  app/
    main.py       FastAPI, routing, cache, rate-limit, model narration
    analysis.py   validation, classification, fetchers, rules engine  <- the brain
    models.py     pydantic schemas (response = what the site renders)
  tests/test_analysis.py
  requirements.txt  .env.example  pytest.ini
```
