"""
Regression suite. The rules-engine tests run with NO network — they are the
guarantee that a clean token reads clean and a rug reads as a rug, on every deploy.

Run:  pytest -q
Integration smoke tests against live addresses are marked @pytest.mark.live and
skipped by default (run with: pytest -m live).
"""
import pytest

from app.analysis import (
    validate_address, extract_address, rules_token, rules_wallet,
    TokenFacts, WalletFacts,
)

# ---------------------------------------------------- address validation -----

def test_validate_real_address():
    assert validate_address("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")  # USDC
    assert validate_address("So11111111111111111111111111111111111111112")  # wSOL

def test_validate_rejects_junk():
    assert not validate_address("not-an-address")
    assert not validate_address("0x1234567890abcdef")          # EVM, not base58/32B
    assert not validate_address("hello world this is a doubt")
    assert not validate_address("I0Ol")                        # base58-illegal chars / short

def test_extract_from_freeform():
    q = "is this safe? EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert extract_address(q) == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert extract_address("what are the signs of a rug pull?") is None


# ---------------------------------------------------- labeled fact fixtures --

def _clean_token():
    return TokenFacts(
        address="X", found=True,
        mint_authority_active=False, freeze_authority_active=False,
        lp_locked=True, top1_pct=4.0, top10_pct=18.0, liquidity_usd=120_000.0,
    )

def _rug_mint_authority():
    f = _clean_token(); f.mint_authority_active = True; return f

def _rug_unlocked_lp():
    f = _clean_token(); f.lp_locked = False; return f

def _rug_concentration():
    f = _clean_token(); f.top1_pct = 71.0; return f

def _honeypot_freeze():
    f = _clean_token(); f.freeze_authority_active = True; return f

def _sparse():
    return TokenFacts(address="X", found=True)  # nothing verified

def _low_liquidity():
    f = _clean_token(); f.liquidity_usd = 3_000.0; f.top10_pct = 30.0; return f

def _bluechip():
    return TokenFacts(address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                      found=True, is_bluechip=True, bluechip_name="USDC")


@pytest.mark.parametrize("factory,expected", [
    (_clean_token,         "emanation"),
    (_bluechip,            "emanation"),
    (_rug_mint_authority,  "illusion"),
    (_rug_unlocked_lp,     "illusion"),
    (_rug_concentration,   "illusion"),
    (_honeypot_freeze,     "illusion"),
    (_sparse,              "veiled"),
    (_low_liquidity,       "veiled"),
])
def test_token_verdicts(factory, expected):
    verdict, title, clarity, signals = rules_token(factory())
    assert verdict == expected, f"{factory.__name__}: got {verdict} ({title})"
    assert 0 <= clarity <= 100
    assert all(s["state"] in ("light", "shadow", "unknown") for s in signals)
    assert all(s["icon"] in
               ("holders", "liquidity", "deployer", "hype", "origin", "snare", "veil")
               for s in signals)

def test_clean_has_high_clarity_and_no_shadows():
    verdict, _, clarity, signals = rules_token(_clean_token())
    assert clarity >= 80
    assert not any(s["state"] == "shadow" for s in signals)

def test_rug_surfaces_a_shadow():
    _, _, _, signals = rules_token(_rug_mint_authority())
    assert any(s["state"] == "shadow" and "Mint" in s["name"] for s in signals)

def test_not_found_token_is_veiled():
    verdict, _, clarity, _ = rules_token(TokenFacts(address="X", found=False))
    assert verdict == "veiled" and clarity < 40


# ---------------------------------------------------------- wallet -----------

def test_wallet_fresh_is_cautious():
    f = WalletFacts(address="X", found=True, sol_balance=0.2, tx_seen=3, age_days=1.0)
    verdict, title, clarity, signals = rules_wallet(f)
    assert verdict in ("veiled", "emanation")
    assert any("freshly created" in s["note"] for s in signals)

def test_wallet_not_found_is_veiled():
    verdict, _, _, _ = rules_wallet(WalletFacts(address="X", found=False))
    assert verdict == "veiled"


# ------------------------------------------------- live smoke (opt-in) -------

@pytest.mark.live
@pytest.mark.asyncio
async def test_live_usdc_reads_emanation():
    import httpx
    from app.analysis import classify, gather_token_facts, rules_token
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    async with httpx.AsyncClient() as c:
        kind, val = await classify(c, usdc)
        assert kind == "token"
        facts = await gather_token_facts(c, usdc, val)
        verdict, *_ = rules_token(facts)
        assert verdict == "emanation"
