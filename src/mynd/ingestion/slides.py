"""Page-aware extraction, without DOI resolution, OCR, or visual interpretation."""
from pathlib import Path

import pymupdf

from mynd.courses.models import SlidePage

# Only normalize known Symbol-font glyphs; never reinterpret arbitrary private-use text.
_SYMBOL = {
    0xF02B: "+", 0xF02D: "−", 0xF03C: "<", 0xF03D: "=", 0xF0A3: "≤",
    0xF0AC: "←", 0xF0B9: "≠", 0xF0EB: "⌊", 0xF0FB: "⌋",
}


def extract_slide_pages(path: Path) -> tuple[SlidePage, ...]:
    pages = []
    with pymupdf.open(path) as pdf:
        if pdf.needs_pass:
            raise ValueError("Password-protected slide PDFs are not supported")
        for number, page in enumerate(pdf, start=1):
            blocks = page.get_text("dict", sort=True)["blocks"]
            lines = []
            normalized = False
            has_images = False
            for block in blocks:
                if block["type"] == 1:
                    has_images = True
                for line in block.get("lines", []):
                    parts = []
                    for span in line["spans"]:
                        text = span["text"]
                        if "symbol" in span["font"].lower():
                            replacement = text.translate(_SYMBOL)
                            normalized |= replacement != text
                            text = replacement
                        parts.append(text)
                    lines.append("".join(parts))
            text = "\n".join(lines).strip()
            warnings = []
            if len(text) < 80:
                warnings.append("Sparse text: this may be a title slide or contain missing content.")
            if has_images:
                warnings.append("Images present: diagrams, code, or equations may be unextracted.")
            if normalized:
                warnings.append("Known Symbol-font glyphs normalized; verify mathematical layout.")
            if any(0xE000 <= ord(c) <= 0xF8FF or c == "\ufffd" for c in text):
                warnings.append("Unresolved glyphs: some mathematical notation may be corrupted.")
            pages.append(SlidePage(number, text, tuple(warnings)))
    if not pages:
        raise ValueError("Slide PDF contains no pages")
    return tuple(pages)
