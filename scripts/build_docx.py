"""Build the Word versions of the submission documents from their Markdown.

    python scripts/build_docx.py
        docs/TECHNICAL_DOCUMENTATION.md -> docs/word/Teman_Technical_Documentation.docx
        docs/security_report.md         -> docs/word/Teman_Security_Self_Assessment.docx

Mermaid diagrams are rendered to PNG (docs/figures/diagrams/), charts are embedded,
links to other repo files become GitHub links, and the table of contents is filled in
with Word when it is installed (Windows). Needs: pip install pypandoc_binary python-docx;
Node.js for the Mermaid CLI (set MMDC to an installed mmdc, otherwise npx downloads it);
Chrome or Edge for rendering (set CHROME to its path if it isn't found).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pypandoc
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUT_DIR = DOCS / "word"
DIAGRAMS = DOCS / "figures" / "diagrams"
GITHUB = "https://github.com/25006675-png/Teman/blob/main/"

JOBS = [
    (DOCS / "TECHNICAL_DOCUMENTATION.md", OUT_DIR / "Teman_Technical_Documentation.docx"),
    (DOCS / "security_report.md", OUT_DIR / "Teman_Security_Self_Assessment.docx"),
]

INK = RGBColor(0x1F, 0x29, 0x37)
MUTED = RGBColor(0x52, 0x51, 0x4E)
BORDER = "BFBFBF"
HEADER_FILL = "F2F2F2"
MAX_W, MAX_H = Cm(17.0), Cm(21.0)  # A4 with 2 cm margins

MERMAID_CONFIG = {
    "theme": "base",
    "themeVariables": {
        "fontFamily": "Segoe UI, Arial, sans-serif", "fontSize": "15px",
        "primaryColor": "#f5f6f8", "primaryBorderColor": "#8a8f98", "primaryTextColor": "#0b0b0b",
        "lineColor": "#5f6368", "secondaryColor": "#ffffff", "tertiaryColor": "#fafafa",
        "clusterBkg": "#fbfbfa", "clusterBorder": "#c9c9c4", "edgeLabelBackground": "#ffffff",
        "actorBkg": "#f5f6f8", "actorBorder": "#8a8f98", "noteBkgColor": "#fff8e1",
    },
    "flowchart": {"curve": "basis", "padding": 12},
}


# ---- Mermaid -----------------------------------------------------------------------------
def find_browser() -> str:
    for candidate in (os.environ.get("CHROME"),
                      r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                      shutil.which("google-chrome"), shutil.which("chromium")):
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("No Chrome/Edge found; set CHROME to a Chromium-based browser.")


def mmdc_command() -> list[str]:
    if os.environ.get("MMDC"):
        return [os.environ["MMDC"]]
    return ["npx", "-y", "@mermaid-js/mermaid-cli@11.4.2"]


def render_mermaid(code: str, name: str, work: Path) -> Path:
    digest = hashlib.sha1((code + json.dumps(MERMAID_CONFIG)).encode()).hexdigest()[:10]
    out = DIAGRAMS / f"{name}-{digest}.png"
    if out.exists():
        return out
    DIAGRAMS.mkdir(parents=True, exist_ok=True)
    src = work / f"{name}.mmd"
    src.write_text(code, encoding="utf-8")
    (work / "config.json").write_text(json.dumps(MERMAID_CONFIG), encoding="utf-8")
    (work / "puppeteer.json").write_text(json.dumps({"executablePath": find_browser().replace("\\", "/"),
                                                     "args": ["--no-sandbox"]}), encoding="utf-8")
    cmd = mmdc_command() + ["-i", str(src), "-o", str(out), "-c", str(work / "config.json"),
                            "-p", str(work / "puppeteer.json"), "-s", "3", "-b", "white", "-w", "1100"]
    subprocess.run(cmd, check=True, capture_output=True, shell=(os.name == "nt" and cmd[0] == "npx"))
    for old in DIAGRAMS.glob(f"{name}-*.png"):  # drop stale renders of the same diagram
        if old != out:
            old.unlink()
    return out


# ---- Markdown preprocessing -----------------------------------------------------------------
def github_link(target: str, md_path: Path) -> str:
    path, _, anchor = target.partition("#")
    resolved = (md_path.parent / path).resolve()
    try:
        rel = resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return target
    kind = "tree" if resolved.is_dir() or path.endswith("/") else "blob"
    url = GITHUB.replace("/blob/", f"/{kind}/") + rel
    return url + (f"#{anchor}" if anchor else "")


def preprocess(md_path: Path, work: Path) -> tuple[str, dict]:
    text = md_path.read_text(encoding="utf-8")
    # title = first H1; the lines before the first horizontal rule become the subtitle block
    title_match = re.match(r"# (.+)\n", text)
    title = title_match.group(1).strip() if title_match else md_path.stem
    text = text[title_match.end():] if title_match else text
    meta = {"title": title}
    subtitle = re.match(r"\s*\*\*([^*\n]+)\*\*\n", text)  # a fully bold first line becomes the subtitle
    if subtitle:
        meta["subtitle"] = subtitle.group(1).strip()
        text = text[subtitle.end():]
    # drop the hand-written Contents list (Word builds its own)
    text = re.sub(r"## Contents\n.*?\n---\n", "", text, count=1, flags=re.S)

    counter = {"n": 0}

    def mermaid(match: re.Match) -> str:
        counter["n"] += 1
        png = render_mermaid(match.group(1), f"{md_path.stem.lower()}-{counter['n']}", work)
        return f"![]({png.as_posix()})"

    text = re.sub(r"```mermaid\n(.*?)\n```", mermaid, text, flags=re.S)

    def image(match: re.Match) -> str:
        alt, target = match.group(1), match.group(2)
        if re.match(r"^[a-zA-Z]:/|^/", target):
            return match.group(0)
        return f"![{alt}]({(md_path.parent / target).resolve().as_posix()})"

    text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", image, text)

    def link(match: re.Match) -> str:
        label, target = match.group(1), match.group(2)
        if target.startswith(("http://", "https://", "mailto:")):
            return match.group(0)
        if target.startswith("#"):
            return label  # in-page anchors differ between GitHub and Word; keep the text
        return f"[{label}]({github_link(target, md_path)})"

    text = re.sub(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"^\n*---\n", "", text.lstrip("\n"), count=1)  # rule right after the intro
    return text, meta


# ---- Word post-processing --------------------------------------------------------------------
def set_cell_border(cell, color: str = BORDER) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for edge in ("top", "left", "bottom", "right"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:color"), color)
        borders.append(el)
    tc_pr.append(borders)


def shade(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def add_page_number(section) -> None:
    paragraph = section.footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if kind:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)
    run.font.size = Pt(9)
    run.font.color.rgb = MUTED


def style_document(path: Path) -> None:
    doc = Document(path)
    for section in doc.sections:
        section.page_width, section.page_height = Cm(21.0), Cm(29.7)
        for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
            setattr(section, side, Cm(2.0))
        add_page_number(section)

    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name, normal.font.size = "Calibri", Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft JhengHei")
    for name in ("Body Text", "First Paragraph", "Compact"):
        if name in [s.name for s in styles]:
            styles[name].font.name, styles[name].font.size = "Calibri", Pt(10.5)
            styles[name].paragraph_format.space_after = Pt(5)
    for name, size in (("Title", 24), ("Subtitle", 12), ("Heading 1", 16), ("Heading 2", 13), ("Heading 3", 11.5)):
        if name in [s.name for s in styles]:
            st = styles[name]
            st.font.name, st.font.size, st.font.bold = "Calibri", Pt(size), name != "Subtitle"
            st.font.color.rgb = INK if name != "Subtitle" else MUTED
            fonts = st.element.rPr.rFonts if st.element.rPr is not None else None
            for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
                if fonts is not None and fonts.get(qn(attr)) is not None:
                    del fonts.attrib[qn(attr)]  # theme fonts would override Calibri
            st.paragraph_format.space_before = Pt(14 if name == "Heading 1" else 10)
            st.paragraph_format.space_after = Pt(4)
    if "Source Code" in [s.name for s in styles]:
        styles["Source Code"].font.size = Pt(8.5)

    for table in doc.tables:
        for r, row in enumerate(table.rows):
            for cell in row.cells:
                set_cell_border(cell)
                if r == 0:
                    shade(cell, HEADER_FILL)
                for paragraph in cell.paragraphs:
                    paragraph.paragraph_format.space_after = Pt(1)
                    for run in paragraph.runs:
                        run.font.size = Pt(9)
                        if r == 0:
                            run.font.bold = True

    for shape in doc.inline_shapes:
        w, h = shape.width, shape.height
        scale = min(MAX_W / w, MAX_H / h, 1.0) if w and h else 1.0
        if w > MAX_W * 0.98 or h > MAX_H:  # big renders: fit the page
            scale = min(MAX_W / w, MAX_H / h)
        shape.width, shape.height = int(w * scale), int(h * scale)
    for paragraph in doc.paragraphs:  # centre paragraphs that only hold an image
        if paragraph._p.xpath(".//w:drawing") and not paragraph.text.strip():
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.save(path)


def word_update_toc(path: Path, pdf: Path | None) -> bool:
    """Fill in the table of contents (and export a PDF preview) with Word, if available."""
    if os.name != "nt":
        return False
    ps = f"""
$ErrorActionPreference = 'Stop'
$w = New-Object -ComObject Word.Application
$w.Visible = $false
try {{
  $d = $w.Documents.Open('{path}')
  foreach ($t in $d.TablesOfContents) {{ $t.Update() }}
  $d.Fields.Update() | Out-Null
  $d.Save()
  {"$d.ExportAsFixedFormat('" + str(pdf) + "', 17)" if pdf else ""}
  $d.Close()
}} finally {{ $w.Quit() }}
"""
    result = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    return result.returncode == 0


def build(md_path: Path, out: Path, pdf_preview: bool) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        text, meta = preprocess(md_path, work)
        src = work / "doc.md"
        src.write_text(text, encoding="utf-8")
        pypandoc.convert_file(
            str(src), "docx", format="gfm", outputfile=str(out),
            extra_args=["--toc", "--toc-depth=2", *[f"--metadata={k}:{v}" for k, v in meta.items()],
                        "--metadata=toc-title:Contents", f"--resource-path={DOCS}"],
        )
    style_document(out)
    pdf = out.with_suffix(".pdf") if pdf_preview else None
    filled = word_update_toc(out, pdf)
    print(f"wrote {out}" + ("" if filled else "  (open in Word and update the table of contents)"))


def main() -> None:
    pdf_preview = "--pdf" in sys.argv
    for md_path, out in JOBS:
        build(md_path, out, pdf_preview)


if __name__ == "__main__":
    main()
