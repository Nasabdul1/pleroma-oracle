"""
The Discernment core (enriched).

Pipeline:
  1. extract + validate a Solana address from the query
  2. classify it on-chain  -> mint (CA) | wallet | not-found
  3. fetch verified facts from RPC + RugCheck + DexScreener + Helius DAS (in parallel)
  4. run a DETERMINISTIC rules engine -> verdict / clarity / signals
  5. build display extras: token lore+socials, top-10 holders, wallet identity

The model never decides the verdict. It narrates the facts this file proves.
Every external response is parsed defensively (.get everywhere): a shape change or
outage degrades a field to None -> lower clarity -> "veiled", never a crash.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import base58
import httpx

# ---------------------------------------------------------------- constants ---

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
RUGCHECK_BASE = os.getenv("RUGCHECK_BASE", "https://api.rugcheck.xyz/v1")
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex"
# Bonfida SNS API for wallet -> .sol domains (reverse lookup). Defensive; optional.
SNS_API = os.getenv("SNS_API", "https://sns-api.bonfida.com")
# Free, no-key token pricing (USD). Up to 50 mints per call.
JUPITER_PRICE = os.getenv("JUPITER_PRICE", "https://lite-api.jup.ag/price/v3")
SOL_MINT = "So11111111111111111111111111111111111111112"
WALLET_TOKENS_SHOWN = 12  # how many holdings to surface in the portfolio box

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "8"))
# Lore (off-chain metadata JSON) lives on IPFS/Arweave. A single gateway is the
# usual reason "lore doesn't show" — ipfs.io rate-limits and times out. Try several.
META_TIMEOUT = float(os.getenv("META_TIMEOUT", "12"))
IPFS_GATEWAYS = [
    "https://ipfs.io/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://nftstorage.link/ipfs/",
    "https://dweb.link/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
]


def _ipfs_to_http(uri: Optional[str]) -> Optional[str]:
    """ipfs://<cid>/path -> first-gateway https URL (browsers can't load ipfs://)."""
    if not uri or not isinstance(uri, str):
        return uri
    u = uri.strip()
    if u.startswith("ipfs://"):
        cid = u[len("ipfs://"):].lstrip("/")
        if cid.startswith("ipfs/"):
            cid = cid[len("ipfs/"):]
        return IPFS_GATEWAYS[0] + cid
    return u


def _ipfs_candidates(uri: str) -> List[str]:
    """All gateway URLs to try for an ipfs:// (or gateway) metadata URI, in order."""
    u = uri.strip()
    cid = None
    if u.startswith("ipfs://"):
        cid = u[len("ipfs://"):].lstrip("/")
        if cid.startswith("ipfs/"):
            cid = cid[len("ipfs/"):]
    elif "/ipfs/" in u:
        cid = u.split("/ipfs/", 1)[1]
    if cid:
        return [g + cid for g in IPFS_GATEWAYS]
    return [u]  # arweave / plain https — fetch as-is

BLUECHIPS = {
    "So11111111111111111111111111111111111111112": "Wrapped SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
}

CONCENTRATION_RUG = 50.0
CONCENTRATION_WARN = 25.0
LIQ_VERY_LOW = 5_000.0
LIQ_LOW = 25_000.0
CAUTION_LIMIT = 2

import re
ADDRESS_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

# ---- Metaplex Token Metadata (works on ANY RPC; the reliable lore source) ----
import hashlib
import base64 as _b64

METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
_ED_P = 2**255 - 19
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P


def _on_curve(b: bytes) -> bool:
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _ED_P:
        return False
    u = (y * y - 1) % _ED_P
    v = (_ED_D * y * y + 1) % _ED_P
    xx = (u * pow(v, _ED_P - 2, _ED_P)) % _ED_P
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P != 0:
        x = (x * pow(2, (_ED_P - 1) // 4, _ED_P)) % _ED_P
    return (x * x - xx) % _ED_P == 0


def _find_pda(seeds, program_id: bytes) -> str:
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program_id
                           + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return base58.b58encode(h).decode()
    raise RuntimeError("no off-curve bump")


def metadata_pda(mint: str) -> str:
    mp = base58.b58decode(METADATA_PROGRAM)
    return _find_pda([b"metadata", mp, base58.b58decode(mint)], mp)


def _read_borsh_str(raw: bytes, off: int):
    ln = int.from_bytes(raw[off:off + 4], "little"); off += 4
    val = raw[off:off + ln].split(b"\x00")[0].decode("utf-8", "ignore").strip()
    return val, off + ln


# ------------------------------------------------------------ validation ---

def validate_address(addr: str) -> bool:
    if not (32 <= len(addr) <= 44):
        return False
    try:
        return len(base58.b58decode(addr)) == 32
    except Exception:
        return False


def extract_address(text: str) -> Optional[str]:
    for candidate in ADDRESS_RE.findall(text.strip()):
        if validate_address(candidate):
            return candidate
    return None


# --------------------------------------------------------------- rpc ---------

async def _rpc(client: httpx.AsyncClient, method: str, params) -> Any:
    r = await client.post(
        SOLANA_RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("result")


async def classify(client: httpx.AsyncClient, addr: str) -> Tuple[str, dict]:
    try:
        res = await _rpc(client, "getAccountInfo", [addr, {"encoding": "jsonParsed"}])
    except Exception:
        return "notfound", {}
    val = (res or {}).get("value")
    if not val:
        return "notfound", {}
    owner = val.get("owner")
    parsed = (val.get("data") or {})
    ptype = parsed.get("parsed", {}).get("type") if isinstance(parsed, dict) else None
    if owner in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) and ptype == "mint":
        return "token", val
    if owner == SYSTEM_PROGRAM:
        return "wallet", val
    return "wallet", val


# ----------------------------------------------------------- facts -----------

@dataclass
class TokenFacts:
    address: str
    found: bool = False
    is_bluechip: bool = False
    bluechip_name: Optional[str] = None
    is_token2022: bool = False
    mint_authority_active: Optional[bool] = None
    freeze_authority_active: Optional[bool] = None
    transfer_fee_bps: Optional[int] = None
    top1_pct: Optional[float] = None
    top10_pct: Optional[float] = None
    liquidity_usd: Optional[float] = None
    price_usd: Optional[float] = None
    market_cap: Optional[float] = None
    volume24h: Optional[float] = None
    lp_locked: Optional[bool] = None
    pair_age_hours: Optional[float] = None
    rugcheck_score: Optional[int] = None
    # --- display extras (lore + socials + holders) ---
    name: Optional[str] = None
    symbol: Optional[str] = None
    image: Optional[str] = None
    description: Optional[str] = None
    twitter: Optional[str] = None
    telegram: Optional[str] = None
    website: Optional[str] = None
    discord: Optional[str] = None
    metadata_uri: Optional[str] = None
    top_holders: List[dict] = field(default_factory=list)   # [{label,pct,flag}]
    sources_ok: List[str] = field(default_factory=list)
    conflict: bool = False


@dataclass
class WalletFacts:
    address: str
    found: bool = False
    sol_balance: Optional[float] = None
    tx_seen: Optional[int] = None
    age_days: Optional[float] = None
    sol_domains: List[str] = field(default_factory=list)
    deployed_count: Optional[int] = None
    # --- portfolio (worth) ---
    sol_price: Optional[float] = None
    sol_usd: Optional[float] = None
    tokens: List[dict] = field(default_factory=list)   # [{mint,symbol,amount,usd,image}]
    token_count: Optional[int] = None
    total_usd: Optional[float] = None
    sources_ok: List[str] = field(default_factory=list)


# ------------------------------------------------------- token fetchers ------

async def _mint_details(client: httpx.AsyncClient, val: dict, f: TokenFacts) -> None:
    info = (val.get("data") or {}).get("parsed", {}).get("info", {})
    program = (val.get("data") or {}).get("program", "")
    f.is_token2022 = program == "spl-token-2022" or val.get("owner") == TOKEN_2022_PROGRAM
    if "mintAuthority" in info:
        f.mint_authority_active = info.get("mintAuthority") is not None
    if "freezeAuthority" in info:
        f.freeze_authority_active = info.get("freezeAuthority") is not None
    for ext in info.get("extensions", []) or []:
        if ext.get("extension") == "transferFeeConfig":
            st = ext.get("state", {})
            newer = st.get("newerTransferFee", {}) or {}
            f.transfer_fee_bps = newer.get("transferFeeBasisPoints")
    f.sources_ok.append("rpc")


async def _largest_accounts(client: httpx.AsyncClient, addr: str, val: dict, f: TokenFacts) -> None:
    try:
        res = await _rpc(client, "getTokenLargestAccounts", [addr])
        info = (val.get("data") or {}).get("parsed", {}).get("info", {})
        supply_raw = float(info.get("supply", 0))
        decimals = int(info.get("decimals", 0))
        supply = supply_raw / (10 ** decimals) if supply_raw else 0
        rows = (res or {}).get("value", [])
        holders = [a.get("uiAmount") or 0 for a in rows]
        if supply > 0 and holders:
            f.top1_pct = round(100 * holders[0] / supply, 2)
            f.top10_pct = round(100 * sum(holders[:10]) / supply, 2)
            # fallback top-10 list (token-account level) only if RugCheck didn't fill it
            if not f.top_holders:
                for a in rows[:10]:
                    amt = a.get("uiAmount") or 0
                    pct = round(100 * amt / supply, 2)
                    acct = a.get("address", "")
                    label = (acct[:4] + "…" + acct[-4:]) if acct else "—"
                    f.top_holders.append({"label": label, "pct": pct,
                                          "flag": "whale" if pct > CONCENTRATION_WARN else ""})
            f.sources_ok.append("holders")
    except Exception:
        pass


async def _dexscreener(client: httpx.AsyncClient, addr: str, f: TokenFacts) -> None:
    try:
        r = await client.get(f"{DEXSCREENER_BASE}/tokens/{addr}", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        if not pairs:
            return
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        f.liquidity_usd = (best.get("liquidity") or {}).get("usd")
        try:
            f.price_usd = float(best.get("priceUsd")) if best.get("priceUsd") else None
        except Exception:
            f.price_usd = None
        f.market_cap = best.get("marketCap") or best.get("fdv")
        f.volume24h = (best.get("volume") or {}).get("h24")
        created = best.get("pairCreatedAt")
        if created:
            f.pair_age_hours = round((time.time() * 1000 - created) / 3_600_000, 1)
        # ---- lore + socials from DexScreener token info ----
        binfo = best.get("info") or {}
        if binfo.get("imageUrl") and not f.image:
            f.image = _ipfs_to_http(binfo.get("imageUrl"))
        for w in binfo.get("websites", []) or []:
            url = w.get("url") if isinstance(w, dict) else w
            if url and not f.website:
                f.website = url
        for soc in binfo.get("socials", []) or []:
            t = (soc.get("type") or "").lower()
            url = soc.get("url")
            if t == "twitter" and not f.twitter:
                f.twitter = _norm_social("twitter", url)
            elif t == "telegram" and not f.telegram:
                f.telegram = _norm_social("telegram", url)
            elif t == "discord" and not f.discord:
                f.discord = _norm_social("discord", url)
        bt = best.get("baseToken") or {}
        if bt.get("name") and not f.name:
            f.name = bt.get("name")
        if bt.get("symbol") and not f.symbol:
            f.symbol = bt.get("symbol")
        f.sources_ok.append("dexscreener")
    except Exception:
        pass


def _norm_social(kind: str, val) -> Optional[str]:
    """Normalize a handle or partial URL into a full link."""
    if not val or not isinstance(val, str):
        return None
    v = val.strip()
    if not v:
        return None
    low = v.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return v
    h = v.lstrip("@").strip("/")
    if kind == "twitter":
        if "twitter.com" in low or "x.com" in low:
            return "https://" + v.replace("https://", "").replace("http://", "")
        return "https://x.com/" + h
    if kind == "telegram":
        if "t.me" in low or "telegram." in low:
            return "https://" + v.replace("https://", "").replace("http://", "")
        return "https://t.me/" + h
    if kind == "discord":
        if "discord" in low:
            return "https://" + v.replace("https://", "").replace("http://", "")
        return "https://discord.gg/" + h
    return "https://" + v.replace("https://", "").replace("http://", "")


def _parse_meta_json(d: dict, f: "TokenFacts") -> None:
    """Scrape ALL lore + socials from a metadata JSON blob (top-level, extensions, links)."""
    if not isinstance(d, dict):
        return
    desc = (d.get("description") or d.get("desc") or d.get("about")
            or d.get("summary") or d.get("bio"))
    if desc and not f.description:
        f.description = str(desc).strip()
    img = (d.get("image") or d.get("image_url") or d.get("imageUrl")
           or d.get("logo") or d.get("icon"))
    if img and not f.image:
        f.image = _ipfs_to_http(img)
    for src in (d, d.get("extensions") or {}, d.get("links") or {}, d.get("properties") or {}):
        if not isinstance(src, dict):
            continue
        tw = src.get("twitter") or src.get("x") or src.get("twitter_url") or src.get("twitterUrl")
        tg = src.get("telegram") or src.get("tg") or src.get("telegram_url")
        web = (src.get("website") or src.get("web") or src.get("site")
               or src.get("homepage") or src.get("external_url") or src.get("url"))
        dc = src.get("discord") or src.get("discord_url")
        if tw and not f.twitter:
            f.twitter = _norm_social("twitter", tw)
        if tg and not f.telegram:
            f.telegram = _norm_social("telegram", tg)
        if web and not f.website:
            f.website = _norm_social("website", web)
        if dc and not f.discord:
            f.discord = _norm_social("discord", dc)
    if d.get("name") and not f.name:
        f.name = d.get("name")
    if d.get("symbol") and not f.symbol:
        f.symbol = d.get("symbol")


async def _metaplex_meta(client: httpx.AsyncClient, addr: str, f: TokenFacts) -> None:
    """Read the on-chain Metaplex metadata account for name/symbol/URI. Any RPC works."""
    try:
        pda = metadata_pda(addr)
        res = await _rpc(client, "getAccountInfo", [pda, {"encoding": "base64"}])
        val = (res or {}).get("value")
        if not val:
            return
        data = val.get("data")
        b64 = data[0] if isinstance(data, list) else data
        raw = _b64.b64decode(b64)
        off = 1 + 32 + 32  # key + update_authority + mint
        name, off = _read_borsh_str(raw, off)
        symbol, off = _read_borsh_str(raw, off)
        uri, off = _read_borsh_str(raw, off)
        if name and not f.name:
            f.name = name
        if symbol and not f.symbol:
            f.symbol = symbol
        if uri and not f.metadata_uri:
            f.metadata_uri = uri
        f.sources_ok.append("metaplex")
    except Exception:
        pass


async def _das_asset(client: httpx.AsyncClient, addr: str, f: TokenFacts) -> None:
    """Helius DAS getAsset -> name/symbol/description/image/links + the json_uri (fetched later)."""
    try:
        res = await _rpc(client, "getAsset", {"id": addr})
        if not isinstance(res, dict):
            return
        content = res.get("content") or {}
        meta = content.get("metadata") or {}
        links = content.get("links") or {}
        if meta.get("name") and not f.name:
            f.name = meta.get("name")
        if meta.get("symbol") and not f.symbol:
            f.symbol = meta.get("symbol")
        if meta.get("description") and not f.description:
            f.description = meta.get("description")
        if links.get("image") and not f.image:
            f.image = links.get("image")
        if links.get("external_url") and not f.website:
            f.website = _norm_social("website", links.get("external_url"))
        # DAS sometimes nests socials in metadata; scrape them
        _parse_meta_json(meta, f)
        if content.get("json_uri") and not f.metadata_uri:
            f.metadata_uri = content.get("json_uri")
        f.sources_ok.append("das")
    except Exception:
        pass


async def _metadata_json(client: httpx.AsyncClient, f: TokenFacts) -> None:
    """Follow the metadata URI and scrape ALL lore + socials (the Rick-style step).

    This is where the token's DESCRIPTION almost always comes from. Tries every
    IPFS gateway in turn so one slow/blocked gateway doesn't kill the lore.
    """
    if not f.metadata_uri:
        return
    for url in _ipfs_candidates(f.metadata_uri):
        try:
            r = await client.get(url, timeout=META_TIMEOUT, follow_redirects=True)
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except Exception:
                # some gateways return text/plain JSON
                import json as _json
                data = _json.loads(r.text)
            _parse_meta_json(data, f)
            f.image = _ipfs_to_http(f.image)  # ensure the logo is browser-loadable
            f.sources_ok.append("metajson")
            return  # got it — stop trying gateways
        except Exception:
            continue


async def _rugcheck(client: httpx.AsyncClient, addr: str, f: TokenFacts) -> None:
    try:
        r = await client.get(f"{RUGCHECK_BASE}/tokens/{addr}/report", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        if not isinstance(d, dict):
            return
        score = d.get("score_normalised", d.get("score"))
        if isinstance(score, (int, float)):
            f.rugcheck_score = int(score)
        markets = d.get("markets") or []
        for m in markets:
            lp = (m.get("lp") or {})
            locked_pct = lp.get("lpLockedPct")
            if locked_pct is not None:
                f.lp_locked = locked_pct >= 90
                break
        # owner-level top holders (preferred over token-account fallback)
        th = d.get("topHolders") or []
        if th:
            f.top_holders = []
            for h in th[:10]:
                pct = h.get("pct")
                owner = h.get("owner") or h.get("address") or ""
                label = (owner[:4] + "…" + owner[-4:]) if owner else "—"
                flag = "insider" if h.get("insider") else ("whale" if (pct or 0) > CONCENTRATION_WARN else "")
                f.top_holders.append({"label": label, "pct": round(pct, 2) if pct else None, "flag": flag})
        # token meta fallback
        tmeta = d.get("tokenMeta") or {}
        if tmeta.get("name") and not f.name:
            f.name = tmeta.get("name")
        if tmeta.get("symbol") and not f.symbol:
            f.symbol = tmeta.get("symbol")
        if tmeta.get("uri") and not f.metadata_uri:
            f.metadata_uri = tmeta.get("uri")
        if f.mint_authority_active is None and "mintAuthority" in d:
            f.mint_authority_active = d.get("mintAuthority") is not None
        if f.freeze_authority_active is None and "freezeAuthority" in d:
            f.freeze_authority_active = d.get("freezeAuthority") is not None
        f.sources_ok.append("rugcheck")
    except Exception:
        pass


async def gather_token_facts(client: httpx.AsyncClient, addr: str, val: dict) -> TokenFacts:
    f = TokenFacts(address=addr, found=True)
    if addr in BLUECHIPS:
        f.is_bluechip = True
        f.bluechip_name = BLUECHIPS[addr]
    await _mint_details(client, val, f)
    # rugcheck first (so its owner-level holders win over the fallback), then the rest parallel
    await _rugcheck(client, addr, f)
    await asyncio.gather(
        _largest_accounts(client, addr, val, f),
        _dexscreener(client, addr, f),
        _das_asset(client, addr, f),
        _metaplex_meta(client, addr, f),
        return_exceptions=True,
    )
    # follow the metadata URI to scrape every social/link (Rick-style "all lore")
    await _metadata_json(client, f)
    return f


# ------------------------------------------------------- wallet fetchers -----

async def _sns_domains(client: httpx.AsyncClient, addr: str, f: WalletFacts) -> None:
    """Reverse-lookup .sol domains owned by this wallet (Bonfida SNS API). Defensive."""
    try:
        r = await client.get(f"{SNS_API}/owners/{addr}/domains", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        names = []
        if isinstance(data, list):
            for d in data:
                n = d.get("domain") if isinstance(d, dict) else d
                if n:
                    names.append(n if str(n).endswith(".sol") else f"{n}.sol")
        f.sol_domains = names[:10]
        if names:
            f.sources_ok.append("sns")
    except Exception:
        pass


# ------------------------------------------------- wallet portfolio (worth) --

def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


async def _jup_prices(client: httpx.AsyncClient, mints: List[str]) -> Dict[str, float]:
    """USD price per mint via Jupiter (free, no key). Missing/illiquid -> absent."""
    out: Dict[str, float] = {}
    for batch in _chunks(list(dict.fromkeys(mints)), 50):
        try:
            r = await client.get(JUPITER_PRICE, params={"ids": ",".join(batch)},
                                  timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                rows = data.get("data", data)  # tolerate {data:{...}} or flat {...}
                for mint, row in (rows or {}).items():
                    p = (row or {}).get("usdPrice") or (row or {}).get("price")
                    if isinstance(p, (int, float)):
                        out[mint] = float(p)
        except Exception:
            continue
    return out


async def _symbols_for_mints(client: httpx.AsyncClient, mints: List[str]) -> Dict[str, str]:
    """Batch on-chain Metaplex symbols for held mints in ONE getMultipleAccounts call."""
    out: Dict[str, str] = {}
    if not mints:
        return out
    try:
        pdas = [metadata_pda(m) for m in mints]
    except Exception:
        return out
    try:
        res = await _rpc(client, "getMultipleAccounts", [pdas, {"encoding": "base64"}])
        rows = (res or {}).get("value", []) or []
        for mint, acct in zip(mints, rows):
            if not acct:
                continue
            try:
                data = acct.get("data")
                b64 = data[0] if isinstance(data, list) else data
                raw = _b64.b64decode(b64)
                off = 1 + 32 + 32
                _name, off = _read_borsh_str(raw, off)
                symbol, off = _read_borsh_str(raw, off)
                if symbol:
                    out[mint] = symbol
            except Exception:
                continue
    except Exception:
        pass
    return out


async def _das_portfolio(client: httpx.AsyncClient, addr: str, f: WalletFacts) -> bool:
    """Helius DAS getAssetsByOwner: balances + USD prices + symbols in one call.

    Only works on a DAS-capable RPC. Returns True if it produced a portfolio.
    """
    try:
        res = await _rpc(client, "getAssetsByOwner", {
            "ownerAddress": addr, "page": 1, "limit": 1000,
            "options": {"showFungible": True, "showNativeBalance": True},
        })
    except Exception:
        return False
    if not isinstance(res, dict):
        return False
    tokens: List[dict] = []
    total = 0.0

    native = res.get("nativeBalance") or {}
    if native.get("lamports") is not None:
        f.sol_balance = round(native["lamports"] / 1e9, 4)
        if native.get("price_per_sol"):
            f.sol_price = float(native["price_per_sol"])
            f.sol_usd = round(f.sol_balance * f.sol_price, 2)
            total += f.sol_usd

    for item in res.get("items", []) or []:
        ti = item.get("token_info") or {}
        bal = ti.get("balance")
        dec = ti.get("decimals")
        if bal is None or dec is None:
            continue
        amount = bal / (10 ** dec)
        if amount <= 0:
            continue
        pi = ti.get("price_info") or {}
        usd = pi.get("total_price")
        if usd is None and pi.get("price_per_token"):
            usd = amount * pi["price_per_token"]
        meta = (item.get("content") or {}).get("metadata") or {}
        links = (item.get("content") or {}).get("links") or {}
        sym = ti.get("symbol") or meta.get("symbol") or (item.get("id", "")[:4])
        tokens.append({
            "mint": item.get("id"), "symbol": sym, "amount": amount,
            "usd": round(usd, 2) if isinstance(usd, (int, float)) else None,
            "image": _ipfs_to_http(links.get("image")),
        })
        if isinstance(usd, (int, float)):
            total += usd

    tokens.sort(key=lambda t: t.get("usd") or 0, reverse=True)
    f.token_count = len(tokens)
    f.tokens = tokens[:WALLET_TOKENS_SHOWN]
    f.total_usd = round(total, 2) if total else f.sol_usd
    f.sources_ok.append("das")
    return bool(tokens) or f.sol_usd is not None


async def _rpc_portfolio(client: httpx.AsyncClient, addr: str, f: WalletFacts) -> None:
    """Public-RPC fallback: token balances via RPC + USD prices via Jupiter."""
    holdings: List[dict] = []  # {mint, amount}
    for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        try:
            res = await _rpc(client, "getTokenAccountsByOwner",
                             [addr, {"programId": program}, {"encoding": "jsonParsed"}])
            for acc in (res or {}).get("value", []) or []:
                info = (((acc.get("account") or {}).get("data") or {})
                        .get("parsed") or {}).get("info") or {}
                mint = info.get("mint")
                amt = (info.get("tokenAmount") or {}).get("uiAmount")
                if mint and amt and amt > 0:
                    holdings.append({"mint": mint, "amount": amt})
        except Exception:
            continue

    # price everything (held mints + SOL) in one batched pass
    mints = [h["mint"] for h in holdings] + [SOL_MINT]
    prices = await _jup_prices(client, mints)

    f.sol_price = prices.get(SOL_MINT)
    if f.sol_balance is not None and f.sol_price:
        f.sol_usd = round(f.sol_balance * f.sol_price, 2)

    total = f.sol_usd or 0.0
    for h in holdings:
        p = prices.get(h["mint"])
        h["usd"] = round(h["amount"] * p, 2) if p else None
        if h["usd"]:
            total += h["usd"]

    holdings.sort(key=lambda t: t.get("usd") or 0, reverse=True)
    f.token_count = len(holdings)
    top = holdings[:WALLET_TOKENS_SHOWN]

    # symbols only for the few we display (one batched RPC call)
    syms = await _symbols_for_mints(client, [h["mint"] for h in top])
    for h in top:
        m = h["mint"]
        h["symbol"] = syms.get(m) or (m[:4] + "…" + m[-4:])
        h["image"] = None
    f.tokens = top
    f.total_usd = round(total, 2) if total else f.sol_usd
    if holdings:
        f.sources_ok.append("token-accounts")


async def _portfolio(client: httpx.AsyncClient, addr: str, f: WalletFacts) -> None:
    """Prefer DAS (one call, with prices+symbols); fall back to RPC + Jupiter."""
    if await _das_portfolio(client, addr, f):
        return
    await _rpc_portfolio(client, addr, f)


async def gather_wallet_facts(client: httpx.AsyncClient, addr: str) -> WalletFacts:
    f = WalletFacts(address=addr, found=True)
    try:
        bal = await _rpc(client, "getBalance", [addr])
        if bal and "value" in bal:
            f.sol_balance = round(bal["value"] / 1e9, 4)
            f.sources_ok.append("rpc")
    except Exception:
        pass
    try:
        sigs = await _rpc(client, "getSignaturesForAddress", [addr, {"limit": 1000}])
        if isinstance(sigs, list):
            f.tx_seen = len(sigs)
            times = [s.get("blockTime") for s in sigs if s.get("blockTime")]
            if times:
                f.age_days = round((time.time() - min(times)) / 86400, 1)
            f.sources_ok.append("signatures")
    except Exception:
        pass
    await asyncio.gather(
        _sns_domains(client, addr, f),
        _portfolio(client, addr, f),
        return_exceptions=True,
    )
    return f


# ----------------------------------------------------- rules engines ---------

def _sig(name, icon, state, note) -> dict:
    return {"name": name, "icon": icon, "state": state, "note": note}


def rules_token(f: TokenFacts) -> Tuple[str, str, int, List[dict]]:
    signals: List[dict] = []

    if not f.found:
        return ("veiled", "VEILED IN SHADOW", 10,
                [_sig("The Origin", "veil", "unknown",
                      "No account for this address on Solana. Nothing to read.")])

    if f.is_bluechip:
        return ("emanation", "EMANATION OF THE FULLNESS", 99,
                [_sig("The Known Light", "origin", "light",
                      f"{f.bluechip_name} — an established, widely-held asset.")])

    major = 0
    cautions = 0
    verified = 0
    total = 5

    if f.mint_authority_active is None:
        signals.append(_sig("The Sealed Mint", "liquidity", "unknown", "Mint authority could not be verified."))
    else:
        verified += 1
        if f.mint_authority_active:
            major += 1
            signals.append(_sig("The Sealed Mint", "liquidity", "shadow",
                                 "Mint authority is ACTIVE — the creator can print unlimited supply."))
        else:
            signals.append(_sig("The Sealed Mint", "liquidity", "light",
                                 "Mint authority is renounced. Supply cannot be inflated."))

    if f.freeze_authority_active is None:
        signals.append(_sig("The Unbound Holder", "liquidity", "unknown", "Freeze authority could not be verified."))
    else:
        verified += 1
        if f.freeze_authority_active:
            major += 1
            signals.append(_sig("The Unbound Holder", "liquidity", "shadow",
                                 "Freeze authority is ACTIVE — your wallet can be frozen from selling."))
        else:
            signals.append(_sig("The Unbound Holder", "liquidity", "light",
                                 "Freeze authority is renounced. Your tokens cannot be frozen."))

    if f.lp_locked is None:
        signals.append(_sig("The Sealed Vault", "liquidity", "unknown", "Liquidity lock status unverified."))
    else:
        verified += 1
        if f.lp_locked:
            signals.append(_sig("The Sealed Vault", "liquidity", "light", "Liquidity is locked or burned."))
        else:
            major += 1
            signals.append(_sig("The Sealed Vault", "liquidity", "shadow",
                                 "Liquidity is UNLOCKED — it can be withdrawn at any moment."))

    if f.top1_pct is None:
        signals.append(_sig("The True Holders", "holders", "unknown", "Holder distribution unverified."))
    else:
        verified += 1
        if f.top1_pct > CONCENTRATION_RUG:
            major += 1
            signals.append(_sig("The True Holders", "holders", "shadow",
                                 f"One wallet holds {f.top1_pct}% of supply — a single hand can crash it."))
        elif (f.top10_pct or 0) > CONCENTRATION_WARN:
            cautions += 1
            signals.append(_sig("The True Holders", "holders", "shadow",
                                 f"Top holders are concentrated (top 10 hold {f.top10_pct}%)."))
        else:
            signals.append(_sig("The True Holders", "holders", "light",
                                 f"Supply is reasonably distributed (top holder {f.top1_pct}%)."))

    if f.liquidity_usd is None:
        signals.append(_sig("The Depth", "liquidity", "unknown",
                            "No tradable pool found — may be pre-launch / bonding curve."))
    else:
        verified += 1
        if f.liquidity_usd < LIQ_VERY_LOW:
            cautions += 1
            signals.append(_sig("The Depth", "liquidity", "shadow",
                                 f"Very thin liquidity (~${int(f.liquidity_usd):,}) — easily manipulated."))
        elif f.liquidity_usd < LIQ_LOW:
            cautions += 1
            signals.append(_sig("The Depth", "liquidity", "shadow",
                                 f"Moderate liquidity (~${int(f.liquidity_usd):,})."))
        else:
            signals.append(_sig("The Depth", "liquidity", "light",
                                 f"Healthy liquidity (~${int(f.liquidity_usd):,})."))

    if f.transfer_fee_bps:
        cautions += 1
        signals.append(_sig("The Closing Snare", "snare", "shadow",
                             f"Token-2022 transfer fee of {f.transfer_fee_bps/100:.2f}% on every move."))

    clarity = round(100 * verified / total)
    if f.conflict:
        clarity = max(0, clarity - 25)

    if major >= 1:
        verdict, title = "illusion", "ILLUSION OF THE DEMIURGE"
    elif clarity < 40:
        verdict, title = "veiled", "VEILED IN SHADOW"
    elif cautions >= CAUTION_LIMIT:
        verdict, title = "veiled", "VEILED IN SHADOW"
    else:
        verdict, title = "emanation", "EMANATION OF THE FULLNESS"

    return verdict, title, clarity, signals


def rules_wallet(f: WalletFacts) -> Tuple[str, str, int, List[dict]]:
    signals: List[dict] = []
    if not f.found:
        return ("veiled", "VEILED IN SHADOW", 10,
                [_sig("The Vessel", "veil", "unknown", "This wallet has no on-chain presence.")])

    if f.sol_balance is not None:
        signals.append(_sig("The Vessel", "holders", "light" if f.sol_balance > 0 else "unknown",
                             f"Holds {f.sol_balance} SOL" +
                             (f" (~${f.sol_usd:,.2f})." if f.sol_usd else ".")))

    # The Treasury — total worth across SOL + all priced tokens.
    if f.total_usd is not None:
        n = f.token_count or 0
        top = f.tokens[0] if f.tokens else None
        lead = (f" Largest position: {top['symbol']}"
                + (f" (~${top['usd']:,.2f})" if top and top.get("usd") else "")
                + ".") if top else ""
        signals.append(_sig("The Treasury", "holders", "light",
                            f"Portfolio worth ~${f.total_usd:,.2f} across {n} "
                            f"token{'s' if n != 1 else ''} + SOL.{lead}"))

    if f.age_days is not None:
        state = "light" if f.age_days > 30 else "shadow"
        signals.append(_sig("The Age", "origin", state,
                             f"First seen ~{f.age_days} days ago" +
                             ("" if f.age_days > 30 else " — freshly created, less trail to judge.")))
    if f.tx_seen is not None:
        signals.append(_sig("The Trail", "deployer", "light" if f.tx_seen > 50 else "unknown",
                             f"~{f.tx_seen} recent signatures observed."))
    if f.sol_domains:
        signals.append(_sig("The Named", "origin", "light",
                             "Owns on-chain identity: " + ", ".join(f.sol_domains[:3])))

    verified = sum(1 for s in signals if s["state"] != "unknown")
    clarity = min(80, round(100 * verified / 4))
    verdict = "veiled" if clarity < 50 else "emanation"
    title = "VEILED IN SHADOW" if verdict == "veiled" else "A VESSEL OF THE FULLNESS"
    return verdict, title, clarity, signals


# ------------------------------------------------- display-extra builders ----

def build_token_meta(f: TokenFacts) -> Optional[dict]:
    """Lore box payload: name, symbol, image, description, socials. None if nothing useful."""
    socials = {k: v for k, v in
               {"twitter": f.twitter, "telegram": f.telegram,
                "website": f.website, "discord": f.discord}.items() if v}
    if not (f.name or f.symbol or f.description or f.image or socials):
        return None
    return {
        "name": f.name, "symbol": f.symbol, "image": f.image,
        "description": f.description, "socials": socials,
    }


def build_token_stats(f: TokenFacts) -> dict:
    """CA + live market data for the contract & market boxes. Always returns the CA."""
    return {
        "ca": f.address,
        "price_usd": f.price_usd,
        "market_cap": f.market_cap,
        "liquidity_usd": f.liquidity_usd,
        "volume24h": f.volume24h,
        "age_hours": f.pair_age_hours,
        "rugcheck_score": f.rugcheck_score,
    }


def build_holders(f: TokenFacts) -> Optional[List[dict]]:
    """Top-10 holders for the UI. None if unverified."""
    return f.top_holders or None


def build_wallet_identity(f: WalletFacts) -> Optional[dict]:
    """Wallet identity + portfolio box: .sol domains, total worth, top holdings, scanned images."""
    images = []
    for t in f.tokens:
        img = t.get("image")
        if img and img not in images:
            images.append(img)
    return {
        "images": images[:12],
        "sol_domains": f.sol_domains,
        "sol_balance": f.sol_balance,
        "sol_usd": f.sol_usd,
        "age_days": f.age_days,
        "total_usd": f.total_usd,
        "token_count": f.token_count,
        "tokens": f.tokens,
        "note": ("On-chain identity only. A wallet's off-chain socials (Twitter, Telegram) "
                 "cannot be cryptographically tied to an address and are not shown — only "
                 "verifiable on-chain names and links are. Worth is priced via Jupiter; "
                 "illiquid tokens with no market price are excluded from the total."),
    }