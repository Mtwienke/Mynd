import json
import subprocess
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pymupdf
import pytest

from mynd.courses.conversion import convert_powerpoint, pptx_slide_count, validate_export
from mynd.courses.pipeline import _prepare_source
from mynd.files import sha256_file


def pptx(path, slides=2, macro=False):
    with ZipFile(path, "w") as archive:
        archive.writestr("ppt/presentation.xml", (
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            "<p:sldIdLst>" + "<p:sldId/>" * slides + "</p:sldIdLst></p:presentation>"
        ))
        if macro:
            archive.writestr("ppt/vbaProject.bin", b"not executable")
    return path


def pdf(path, pages=2):
    with pymupdf.open() as doc:
        for _ in range(pages):
            doc.new_page()
        doc.save(path)


def test_export_uses_only_staged_copies_and_validates_slide_count(tmp_path):
    source = pptx(tmp_path / 'A strange "name".pptx')
    before = source.read_bytes()
    destination = tmp_path / "library" / "slides.pdf"

    def export(args, **kwargs):
        staged, output = args[2:4]
        assert staged != str(source)
        assert 'A strange' not in kwargs["input"]  # Paths are arguments, never script text.
        assert "active presentation" not in kwargs["input"]
        assert "quit" not in kwargs["input"]
        assert "candidatePath" in kwargs["input"]  # Never close an unrelated user presentation.
        # PowerPoint overwrites the pre-created sandbox-granted destination alias.
        Path(output).unlink()
        pdf(output)
        return subprocess.CompletedProcess(args, 0, "", "")

    with (patch("mynd.courses.conversion.powerpoint_available", return_value=True),
          patch("mynd.courses.conversion.subprocess.run", side_effect=export)):
        assert convert_powerpoint(source, destination) == destination
    assert source.read_bytes() == before
    validate_export(destination, 2)


@pytest.mark.parametrize("error, message", [
    (subprocess.TimeoutExpired("osascript", 200), "timed out"),
    (subprocess.CalledProcessError(1, "osascript", stderr="Not authorized"), "Automation"),
])
def test_converter_failures_do_not_modify_original_or_publish_pdf(tmp_path, error, message):
    source = pptx(tmp_path / "slides.pptx")
    before = source.read_bytes()
    output = tmp_path / "slides.pdf"
    with (patch("mynd.courses.conversion.powerpoint_available", return_value=True),
          patch("mynd.courses.conversion.subprocess.run", side_effect=error),
          pytest.raises(ValueError, match=message)):
        convert_powerpoint(source, output)
    assert not output.exists()
    assert source.read_bytes() == before


def test_missing_powerpoint_has_actionable_error(tmp_path):
    source = pptx(tmp_path / "slides.pptx")
    with (patch("mynd.courses.conversion.powerpoint_available", return_value=False),
          pytest.raises(ValueError, match="export.*manually")):
        convert_powerpoint(source, tmp_path / "slides.pdf")


def test_mismatched_page_count_is_rejected(tmp_path):
    path = tmp_path / "handout.pdf"
    pdf(path, pages=1)
    with pytest.raises(ValueError, match="potentially incorrect page references"):
        validate_export(path, 2)


def test_macro_bearing_or_invalid_pptx_rejected(tmp_path):
    with pytest.raises(ValueError, match="Macro-bearing"):
        pptx_slide_count(pptx(tmp_path / "macro.pptx", macro=True))
    bad = tmp_path / "broken.pptx"
    bad.write_text("not a ZIP")
    with pytest.raises(ValueError, match="Invalid PowerPoint"):
        pptx_slide_count(bad)


def test_cached_pdf_conversion_is_reused_but_tampering_invalidates_it(tmp_path):
    source = pptx(tmp_path / "slides.pptx")
    digest = sha256_file(source)

    def export(_source, destination):
        pdf(destination)
        return destination

    with patch("mynd.courses.pipeline.convert_powerpoint", side_effect=export) as converter:
        _, output = _prepare_source(source, digest, tmp_path / "data")
        _prepare_source(source, digest, tmp_path / "data")
        assert converter.call_count == 1
        manifest = output.with_name("conversion.json")
        manifest.write_text(json.dumps({"pdf_sha256": "wrong"}))
        # Remove our synthetic export only; a real export writes a new staged PDF first.
        output.unlink()
        _prepare_source(source, digest, tmp_path / "data")
        assert converter.call_count == 2
