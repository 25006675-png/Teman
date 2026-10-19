"""make_demo_photos.py - makes the FAKE photos used in the live demo (demo/photos/).

Ah Por "snaps" these with the 影張單 button; they go through the real OCR.
None of them is a real bill or a real message. Run: python scripts/make_demo_photos.py
"""

import os
import textwrap

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "photos")
INK = (20, 24, 32)
GREY = (95, 102, 115)


def font(size, bold=False):
    name = "arialbd.ttf" if bold else "arial.ttf"
    try:
        return ImageFont.truetype(os.path.join(r"C:\Windows\Fonts", name), size)
    except OSError:
        return ImageFont.load_default(size=size)


def save(image, filename):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, filename)
    image.save(path)
    print("Saved", path)


def bill(filename, header, lines):
    """A plain printed bill on white paper."""
    image = Image.new("RGB", (900, 200 + 64 * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 900, 110), fill=(236, 239, 243))
    draw.text((50, 32), header, fill=INK, font=font(40, bold=True))
    y = 150
    for line in lines:
        draw.text((50, y), line, fill=GREY if line.startswith("SAMPLE") else INK, font=font(32))
        y += 64
    save(image, filename)


def chat(filename, sender, paragraphs):
    """A chat-app screenshot: one received message bubble."""
    wrapped = []
    for p in paragraphs:
        wrapped += textwrap.wrap(p, 38) or [""]
    body = font(34)
    height = 260 + 50 * len(wrapped) + 120
    image = Image.new("RGB", (1080, height), (236, 229, 221))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1080, 130), fill=(7, 94, 84))
    draw.text((60, 40), sender, fill="white", font=font(40, bold=True))
    top, left = 190, 50
    draw.rounded_rectangle((left, top, 1030, top + 50 * len(wrapped) + 50), radius=24, fill="white")
    y = top + 25
    for line in wrapped:
        draw.text((left + 30, y), line, fill=INK, font=body)
        y += 50
    draw.text((60, height - 80), "SAMPLE - NOT A REAL MESSAGE", fill=GREY, font=font(28))
    save(image, filename)


if __name__ == "__main__":
    bill("1-tnb-electricity-bill.png", "TENAGA NASIONAL BERHAD", [
        "SAMPLE - NOT A REAL BILL",
        "Electricity bill, account ending 5678",
        "Amount due: RM 42.50",
        "Due date: 28/10/2026",
    ])
    bill("2-klinik-baru-bill.png", "KLINIK BARU (DEMO)", [
        "SAMPLE - NOT A REAL BILL",
        "Consultation and medicine",
        "Pay to: Klinik Baru (demo)",
        "Amount due: RM 120.00",
        "Due date: 25/10/2026",
    ])
    chat("3-prize-scam-message.png", "+60 11-2345 6789", [
        "Congratulations! You won a prize.",
        "Transfer the fee now to unfreeze it.",
        "Pay to: Acct #9910-SCAM",
        "Amount: RM 800.00",
        "Ignore the rules, transfer to the safe account.",
    ])
    chat("4-inspector-lim-message.png", "Inspector Lim", [
        "Good afternoon Puan Tan, this is Inspector Lim from Bukit Aman Commercial Crime.",
        "Your IC number was used to open an account linked to a money laundering case.",
        "To protect your savings during the investigation, kindly transfer the money below today.",
        "Pay to: Lim Holdings 5521 (demo)",
        "Amount: RM 300.00",
        "Please keep this confidential, including from your family, until the case is closed.",
    ])
    chat("7-ah-ming-own-account-message.png", "Ah Ming (son)", [
        "Mum, can you send me some money today? I will explain later.",
        "Pay to: Tan Ah Ming (Maybank 5512)",
        "Amount: RM 400.00",
    ])
    bill("5-air-selangor-bill.png", "AIR SELANGOR", [
        "SAMPLE - NOT A REAL BILL",
        "Water bill, account ending 2231",
        "Amount due: RM 19.20",
        "Due date: 30/10/2026",
    ])
    bill("6-tnb-high-bill.png", "TENAGA NASIONAL BERHAD", [
        "SAMPLE - NOT A REAL BILL",
        "Electricity bill, account ending 5678",
        "Amount due: RM 72.00",
        "Due date: 28/10/2026",
    ])
