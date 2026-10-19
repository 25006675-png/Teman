"""make_screenshots.py - captures the app's key moments as PNGs for the slide deck (docs/screenshots/).

Starts its own server (temporary database; the AI check is on if GEMINI_API_KEY is set),
drives the real page in a browser with a simulated microphone, and saves each phone or
panel at slide resolution. Needs: playwright (+ chromium), Tesseract, Whisper large-v3,
internet for the voice, and eval/speech_audio/ from `python eval/run_speech_eval.py`.

Run from the repo root (about 3 minutes):
    python scripts/make_screenshots.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
PHOTOS = ROOT / "demo" / "photos"
VOICE = ROOT / "eval" / "speech_audio" / "i1_zh-HK-HiuGaaiNeural_clean.wav"  # 「我仲有幾多錢？」
PORT = 8778
URL = f"http://127.0.0.1:{PORT}/"


def main() -> None:
    if not VOICE.exists():
        raise SystemExit(f"Missing {VOICE}: run python eval/run_speech_eval.py first (it makes the voice clips).")
    OUT.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "DIALECTGUARD_DB_PATH": str(Path(tempfile.mkdtemp(prefix="teman_shots_")) / "s.sqlite3"),
           "DIALECTGUARD_SCHEDULER": "off"}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PORT),
                               "--log-level", "warning"], cwd=ROOT, env=env)
    try:
        for _ in range(120):
            try:
                httpx.get(URL + "state", timeout=2); break
            except httpx.HTTPError:
                time.sleep(0.5)
        time.sleep(20)  # let the speech model finish loading in the background
        shoot()
    finally:
        server.terminate()
        server.wait(timeout=20)


def shoot() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                          f"--use-file-for-fake-audio-capture={VOICE}", "--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 900}, device_scale_factor=2, permissions=["microphone"])
        pg = ctx.new_page()
        w = pg.wait_for_timeout
        her = pg.locator("main.grid > .col").nth(0).locator(".phone")
        son = pg.locator("main.grid > .col").nth(1).locator(".phone")
        engine, bank = pg.locator(".panel.engine"), pg.locator(".panel.bank")

        def shot(locator, name):
            locator.screenshot(path=str(OUT / name))
            print("saved", name)

        def wait_for(text, where="#porStage", secs=40):
            t0 = time.time()
            while time.time() - t0 < secs:
                if text in pg.inner_text(where):
                    return
                w(300)
            print("  (timed out waiting for", text, ")")

        def snap(photo):
            pg.locator("#photoInput").set_input_files(str(PHOTOS / photo))

        def hold(selector, ms=1200):
            box = pg.locator(selector).bounding_box()
            pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            pg.mouse.down(); w(ms); pg.mouse.up()

        pg.goto(URL); w(1500)
        pg.click("#resetBtn"); w(2000)
        shot(her, "01-ah-por-home.png")
        shot(son, "02-ah-ming-home.png")

        # Just ask: hold the mic and speak Cantonese
        box = pg.locator("#porMic").bounding_box()
        pg.mouse.move(box["x"] + 50, box["y"] + 50); pg.mouse.down(); w(1800)
        shot(her, "03-listening.png")
        w(1700); pg.mouse.up()
        wait_for("你戶口有")
        w(800); shot(her, "04-balance-answer.png")

        # Snap a bill: confirm sheet, pay now
        snap("1-tnb-electricity-bill.png"); wait_for("要唔要"); w(800)
        shot(her, "05-bill-confirm.png")
        hold("[data-hold=pay]"); w(700)
        shot(her, "06-paying-undo.png")
        wait_for("交咗喇", secs=15); w(800)
        shot(her, "07-paid.png")

        # Spending insights (typed example, same engine path as speech)
        pg.click("[data-act=vp-type]"); w(600)
        pg.click(".vp-ex:has-text('今個星期')"); wait_for("今個星期你"); w(800)
        shot(her, "08-weekly-report.png")

        # Pay later
        snap("5-air-selangor-bill.png"); wait_for("要唔要"); w(800)
        pg.click("[data-act=pay-later]"); wait_for("我記住咗"); w(800)
        shot(her, "09-pay-later-saved.png")

        # Family co-approval
        snap("2-klinik-baru-bill.png"); wait_for("問咗阿明"); w(1200)
        shot(her, "10-her-waiting.png")
        shot(son, "11-his-request.png")
        shot(engine, "12-engine-review-decision.png")
        pg.click(".son-take [data-act=son-approve]"); w(500)
        pg.keyboard.type("25"); w(500)
        shot(son, "13-pin-pad.png")
        pg.keyboard.type("80"); wait_for("阿明批准咗", secs=15); w(1200)
        shot(bank, "14-bank-signed-approvals.png")

        # Scam with a hidden instruction: the red screen
        snap("3-prize-scam-message.png"); pg.wait_for_selector(".vp-takeover", timeout=30000); w(1500)
        shot(her, "15-scam-red-screen.png")
        shot(son, "16-his-scam-banner.png")
        pg.screenshot(path=str(OUT / "00-split-screen-scam.png")); print("saved 00-split-screen-scam.png")
        pg.click("[data-act=son-report]"); w(1200)
        pg.click(".vp-takeover [data-act=dismiss]"); w(1000)

        # Scam with no keywords: the second AI's tactics on his screen
        snap("4-inspector-lim-message.png"); pg.wait_for_selector(".son-take", timeout=40000)
        try:
            pg.wait_for_selector(".son-take .ai-chip", timeout=30000)
        except Exception:
            print("  (no AI tactics: is GEMINI_API_KEY set and the internet on?)")
        w(1000)
        shot(son, "17-his-request-ai-tactics.png")
        pg.click(".son-take [data-act=son-scam]"); w(1500)

        # Family can be the problem too
        snap("7-ah-ming-own-account-message.png"); pg.wait_for_selector(".son-take .selfpay", timeout=40000); w(1200)
        shot(son, "18-his-own-account-blocked.png")
        shot(her, "19-her-own-account-message.png")
        pg.click(".son-take [data-act=son-reject]"); w(1500)

        # Photo cross-check: an amount the biller never billed
        snap("6-tnb-high-bill.png"); wait_for("唔夾"); w(800)
        shot(her, "20-photo-amount-mismatch.png")

        browser.close()


if __name__ == "__main__":
    main()
