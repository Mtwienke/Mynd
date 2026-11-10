import base64
from unittest.mock import MagicMock, patch

import pymupdf
import pytest

from mynd.ingestion.slides import extract_slide_pages


def test_extraction_keeps_page_numbers_and_detects_images(tmp_path):
    path = tmp_path / "slides.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((40, 40), "Optimization defines an objective and constraints.")
        page = pdf.new_page()
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS1cAAAAASUVORK5CYII="
        )
        page.insert_image(pymupdf.Rect(0, 0, 50, 50), stream=png)
        pdf.save(path)
    pages = extract_slide_pages(path)
    assert [p.number for p in pages] == [1, 2]
    assert "objective" in pages[0].text
    assert "Sparse text" in pages[1].warnings[0]
    assert any("Images present" in warning for warning in pages[1].warnings)


def test_known_symbol_font_glyphs_normalized_unknown_glyphs_flagged(tmp_path):
    pdf = MagicMock()
    pdf.__enter__.return_value = pdf
    pdf.needs_pass = False
    page = MagicMock()
    page.get_text.return_value = {"blocks": [{"type": 0, "lines": [{"spans": [
        {"font": "SymbolMT", "text": "x\uf0a3n\uf0ac1\uf0ea"},
        {"font": "OtherFont", "text": "\uf0a3"},
    ]}]}]}
    pdf.__iter__.return_value = iter([page])
    with patch("mynd.ingestion.slides.pymupdf.open", return_value=pdf):
        result = extract_slide_pages(tmp_path / "slides.pdf")[0]
    assert "x≤n←1" in result.text
    assert "\uf0a3" in result.text  # Never reinterpret an unknown font's private-use encoding.
    assert any("Unresolved glyphs" in warning for warning in result.warnings)
    assert any("normalized" in warning for warning in result.warnings)


def test_encrypted_slide_pdf_reports_password_requirement(tmp_path):
    path = tmp_path / "encrypted.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                 owner_pw="owner", user_pw="secret")
    with pytest.raises(ValueError, match="Password-protected"):
        extract_slide_pages(path)
