"""
Pitch screenshots for PLEROMA.

Starts a Playwright browser, walks every page of the site, drives the Oracle on
the homepage to produce a verdict, and saves PNGs to ./screenshots/.

Requires the backend running at http://127.0.0.1:8765 (uvicorn).

Usage:
    python scripts/shoot.py
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

BASE = "http://127.0.0.1:8765"
OUT = Path(__file__).resolve().parent.parent / "screenshots"
OUT.mkdir(exist_ok=True)

# A real Solana token mint with rich on-chain data — dogwifhat (WIF).
DEMO_TOKEN = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
# A wallet with real activity — known Magic Eden hot wallet.
DEMO_WALLET = "GcuxAvTz9SsEaWf9hLfjbrDGpeu4MMHTYHCm8aDdJaJS"

# 1440x900 — close to MacBook viewport, looks crisp on socials.
VIEWPORT = {"width": 1440, "height": 900}


async def shoot(page, path: Path, full_page: bool = True):
    await page.wait_for_timeout(900)  # let animations / lazy data settle
    await page.screenshot(path=str(path), full_page=full_page)
    print(f"  -> {path.name}")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
        page = await ctx.new_page()

        # ---------- 1) Oracle home (empty console) ----------
        print("[1] Oracle home (empty)")
        await page.goto(f"{BASE}/", wait_until="networkidle")
        await page.wait_for_selector("#query")
        await shoot(page, OUT / "01-oracle-home.png")

        # ---------- 2) Oracle home with a real verdict (token) ----------
        print("[2] Oracle home (with verdict)")
        await page.fill("#query", DEMO_TOKEN)
        await page.click("#ask")
        # wait for verdict to render
        try:
            await page.wait_for_selector("#verdict.on", timeout=60_000)
            await page.wait_for_timeout(1500)
        except PWTimeout:
            print("  ! verdict did not appear, screenshotting anyway")
        await shoot(page, OUT / "02-oracle-verdict-token.png")

        # ---------- 3) Oracle home with a question verdict ----------
        print("[3] Oracle home (question verdict)")
        await page.goto(f"{BASE}/", wait_until="networkidle")
        await page.fill("#query", "is a token with 60% in top 10 wallets safe?")
        await page.click("#ask")
        try:
            await page.wait_for_selector("#verdict.on", timeout=60_000)
            await page.wait_for_timeout(1500)
        except PWTimeout:
            print("  ! verdict did not appear")
        await shoot(page, OUT / "03-oracle-question.png")

        # ---------- 4) Token permalink page ----------
        print("[4] Token permalink")
        await page.goto(f"{BASE}/token/{DEMO_TOKEN}", wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("#content", state="visible", timeout=90_000)
            await page.wait_for_timeout(4000)  # let chart iframe + holder bars settle
        except PWTimeout:
            print("  ! token page did not finish loading")
        await shoot(page, OUT / "04-token-permalink.png")

        # ---------- 5) Wallet permalink page ----------
        print("[5] Wallet permalink")
        await page.goto(f"{BASE}/wallet/{DEMO_WALLET}", wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("#content", state="visible", timeout=90_000)
            await page.wait_for_timeout(3000)
        except PWTimeout:
            print("  ! wallet page did not finish loading")
        await shoot(page, OUT / "05-wallet-permalink.png")

        # ---------- 6) Leaderboard ----------
        print("[6] Leaderboard")
        await page.goto(f"{BASE}/leaderboard", wait_until="networkidle")
        await page.wait_for_timeout(2500)  # auto-load happens on mount
        await shoot(page, OUT / "06-leaderboard.png")

        # ---------- 7) Alerts ----------
        print("[7] Alerts")
        await page.goto(f"{BASE}/alerts", wait_until="networkidle")
        await page.wait_for_timeout(2500)
        await shoot(page, OUT / "07-alerts.png")

        # ---------- 8) Communion ----------
        print("[8] Communion")
        await page.goto(f"{BASE}/communion", wait_until="networkidle")
        # seed a couple of fake messages so the page isn't empty in the screenshot
        await page.evaluate(
            """async () => {
                await fetch('/communion/post', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({name:'AeonSeeker', text:'just ran this new launch through the Oracle — Illusion, clarity 78. dodged a bullet.'})
                });
                await new Promise(r=>setTimeout(r,200));
                await fetch('/communion/post', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({name:'GnosisDev', text:'permalink share-card pngs are 🔥. dropping one in the TG now.'})
                });
                await new Promise(r=>setTimeout(r,200));
                await fetch('/communion/post', {
                    name:'TheVeiledOne', text:'reading my own wallet feels like therapy honestly'
                });
                await new Promise(r=>setTimeout(r,200));
                await fetch('/communion/post', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({name:'TheVeiledOne', text:'reading my own wallet feels like therapy honestly'})
                });
            }"""
        )
        await page.wait_for_timeout(3500)
        await shoot(page, OUT / "08-communion.png")

        # ---------- 9) Docs ----------
        print("[9] Docs")
        await page.goto(f"{BASE}/docs", wait_until="networkidle")
        await page.wait_for_timeout(1500)
        await shoot(page, OUT / "09-docs.png")

        # ---------- 10) Docs - pipeline section close-up ----------
        print("[10] Docs - how it works section")
        await page.goto(f"{BASE}/docs#how-it-works", wait_until="networkidle")
        await page.wait_for_timeout(1500)
        # screenshot just the section, not full page
        section = page.locator("#how-it-works")
        await section.screenshot(path=str(OUT / "10-docs-pipeline.png"))
        print(f"  -> 10-docs-pipeline.png")

        await browser.close()
        print()
        print("Done. Screenshots saved to:", OUT)


if __name__ == "__main__":
    asyncio.run(main())
