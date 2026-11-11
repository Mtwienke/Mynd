import json
import subprocess
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pymupdf
import pytest

from mynd.courses.conversion import (
    _CLEANUP_SCRIPT,
    _EXPORT_SCRIPT,
    convert_powerpoint,
    pptx_slide_count,
    validate_export,
)
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
        if kwargs["input"] == _CLEANUP_SCRIPT:
            assert kwargs["timeout"] == 10
            assert destination.is_file()  # Export is durable before cleanup starts.
            return subprocess.CompletedProcess(args, 0, "closed staged presentation", "")
        assert kwargs["input"] == _EXPORT_SCRIPT
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
          patch("mynd.courses.conversion.subprocess.run", side_effect=export) as run):
        assert convert_powerpoint(source, destination) == destination
    assert run.call_count == 2
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
          patch("mynd.courses.conversion.subprocess.run", side_effect=[
              error, subprocess.CompletedProcess("cleanup", 0, "", ""),
          ]),
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

    def export(_source, destination, **_kwargs):
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


def test_timeout_reports_last_powerpoint_stage_and_preserves_partial_stderr(tmp_path):
    source = pptx(tmp_path / "slides.pptx")
    error = subprocess.TimeoutExpired(
        "osascript", 200, stderr=b"(*MYND_EXPORT_STAGE: opening staged presentation*)\n",
    )
    messages = []
    with (patch("mynd.courses.conversion.powerpoint_available", return_value=True),
          patch("mynd.courses.conversion.subprocess.run", side_effect=[
              error, subprocess.CompletedProcess("cleanup", 0, "", ""),
          ]),
          pytest.raises(ValueError, match="last stage: opening staged presentation")):
        convert_powerpoint(source, tmp_path / "slides.pdf", debug=messages.append)
    output = "\n".join(messages)
    assert "process timeout=200s" in output
    assert "MYND_EXPORT_STAGE: opening staged presentation" in output
    assert "timed out after" in output
    assert not (tmp_path / "slides.pdf").exists()


def test_conversion_cache_reports_hit_in_debug_output(tmp_path):
    source = pptx(tmp_path / "slides.pptx")
    digest = sha256_file(source)
    messages = []

    def export(_source, destination, **_kwargs):
        pdf(destination)
        return destination

    with patch("mynd.courses.pipeline.convert_powerpoint", side_effect=export) as converter:
        _prepare_source(source, digest, tmp_path / "data", debug=messages.append)
        _prepare_source(source, digest, tmp_path / "data", debug=messages.append)
    assert converter.call_count == 1
    assert any("Reusing hash-verified converted PDF" in message for message in messages)
    assert any("Saved conversion manifest" in message for message in messages)


def test_cleanup_uses_bounded_index_iteration_not_live_collection_iteration():
    assert "repeat with candidate in presentations" not in _CLEANUP_SCRIPT
    assert "set presentationCount to count of presentations" in _CLEANUP_SCRIPT
    assert "repeat with presentationIndex from presentationCount to 1 by -1" in _CLEANUP_SCRIPT
    assert "set candidatePath to (get full name of candidate) as text" in _CLEANUP_SCRIPT
    assert "set candidateFile to (POSIX file candidatePath) as alias" in _CLEANUP_SCRIPT
    assert "candidateFile is inputFile or candidateFile is outputFile" in _CLEANUP_SCRIPT
    assert "with timeout of 5 seconds" in _CLEANUP_SCRIPT
    assert "my ownsPresentation(candidate, inputFile, outputFile)" in _CLEANUP_SCRIPT
    assert "close candidate saving no" not in _EXPORT_SCRIPT
    assert "quit" not in _CLEANUP_SCRIPT


def test_export_uses_file_specifier_not_alias_for_save_destination():
    # Native PowerPoint can return success while leaving a zero-byte file for an alias.
    assert "save targetPresentation in (POSIX file (item 2 of argv)) as save as PDF" in _EXPORT_SCRIPT
    assert "save targetPresentation in outputFile as save as PDF" not in _EXPORT_SCRIPT


@pytest.mark.parametrize("cleanup_error", [
    subprocess.TimeoutExpired("osascript cleanup", 10),
    subprocess.CalledProcessError(1, "osascript cleanup", stderr="Cleanup blocked"),
    OSError("Could not start cleanup process"),
])
def test_cleanup_failure_keeps_a_validated_export_successful(tmp_path, caplog, cleanup_error):
    source = pptx(tmp_path / "slides.pptx")
    before = source.read_bytes()
    destination = tmp_path / "slides.pdf"

    def run(args, **kwargs):
        if kwargs["input"] == _CLEANUP_SCRIPT:
            assert kwargs["timeout"] == 10
            validate_export(destination, 2)
            raise cleanup_error
        output = Path(args[3])
        output.unlink()
        pdf(output)
        return subprocess.CompletedProcess(args, 0, "", "MYND_EXPORT_STAGE: export complete")

    with (patch("mynd.courses.conversion.powerpoint_available", return_value=True),
          patch("mynd.courses.conversion.subprocess.run", side_effect=run)):
        assert convert_powerpoint(source, destination) == destination
    validate_export(destination, 2)
    assert source.read_bytes() == before
    assert "cleanup failed" in caplog.text
    assert "does not invalidate" in caplog.text


def test_cleanup_failure_never_masks_original_export_error_or_overwrites_previous_pdf(tmp_path):
    source = pptx(tmp_path / "slides.pptx")
    destination = tmp_path / "slides.pdf"
    pdf(destination)
    before = destination.read_bytes()
    export_error = subprocess.CalledProcessError(1, "export", stderr="Export permission denied")
    with (patch("mynd.courses.conversion.powerpoint_available", return_value=True),
          patch("mynd.courses.conversion.subprocess.run", side_effect=[
              export_error, subprocess.TimeoutExpired("cleanup", 10),
          ]),
          pytest.raises(ValueError, match="Export permission denied")):
        convert_powerpoint(source, destination)
    assert destination.read_bytes() == before


@pytest.mark.parametrize("exported_pages", [None, 1])
def test_cleanup_failure_cannot_turn_invalid_or_empty_pdf_into_success(tmp_path, exported_pages):
    source = pptx(tmp_path / "slides.pptx")
    destination = tmp_path / "slides.pdf"

    def run(args, **kwargs):
        if kwargs["input"] == _CLEANUP_SCRIPT:
            raise subprocess.TimeoutExpired("cleanup", 10)
        if exported_pages is not None:
            output = Path(args[3])
            output.unlink()
            pdf(output, pages=exported_pages)
        return subprocess.CompletedProcess(args, 0, "", "")

    with (patch("mynd.courses.conversion.powerpoint_available", return_value=True),
          patch("mynd.courses.conversion.subprocess.run", side_effect=run),
          pytest.raises(ValueError)):
        convert_powerpoint(source, destination)
    assert not destination.exists()
