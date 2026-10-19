"""make_sample_bills.py - makes FAKE bill images for testing.
These are not real bills and not a real TNB layout."""

import os
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "tests", "sample_bills")


def get_font(size):
    """Try the Windows Arial font; if missing, use Pillow's default."""
    try:
        return ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def draw_bill(lines, filename):
    """Draw each line of text on a white image and save it as a PNG."""
    os.makedirs(OUT_DIR, exist_ok=True)
    font = get_font(32)
    image = Image.new("RGB", (900, 120 + 60 * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    y = 40
    for line in lines:
        draw.text((50, y), line, fill="black", font=font)
        y += 60
    path = os.path.join(OUT_DIR, filename)
    image.save(path)
    print("Saved", path)


if __name__ == "__main__":
    draw_bill([
        "TENAGA NASIONAL BERHAD",
        "ELECTRICITY BILL (SAMPLE - NOT REAL)",
        "Account No: 0000-0000-00",
        "Customer: SAMPLE CUSTOMER",
        "Bill Date: 01/10/2026",
        "Amount Due: RM 42.50",
        "Due Date: 25/10/2026",
    ], "bill_normal.png")