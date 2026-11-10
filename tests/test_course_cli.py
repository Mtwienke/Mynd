from pathlib import Path
from unittest.mock import MagicMock, patch

import typer
from typer.testing import CliRunner

from mynd.cli import app
from mynd.courses.models import Course, CourseProposal, Evidence
from mynd.courses.review import review_assignment

runner = CliRunner()


def test_slides_only_does_not_start_paper_services(config_file):
    with (patch("mynd.cli.ingest_inbox") as papers,
          patch("mynd.cli.CrossrefClient") as crossref,
          patch("mynd.cli.ingest_slides", return_value=[{"status": "ready"}]) as slides):
        result = runner.invoke(app, ["ingest", "--kind", "slides", "--non-interactive",
                                     "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    papers.assert_not_called()
    crossref.assert_not_called()
    assert slides.call_args.args[1] is None


def test_default_ingest_processes_both_inboxes(config_file):
    with (patch("mynd.cli.ingest_inbox", return_value=[]) as papers,
          patch("mynd.cli.CrossrefClient", return_value=MagicMock()),
          patch("mynd.cli.ingest_slides", return_value=[]) as slides):
        result = runner.invoke(app, ["ingest", "--config", str(config_file)])
    assert result.exit_code == 0
    papers.assert_called_once()
    slides.assert_called_once()


def test_papers_only_keeps_existing_workflow(config_file):
    with (patch("mynd.cli.ingest_inbox", return_value=[]),
          patch("mynd.cli.CrossrefClient", return_value=MagicMock()),
          patch("mynd.cli.ingest_slides") as slides):
        result = runner.invoke(app, ["ingest", "--kind", "papers", "--config", str(config_file)])
    assert result.exit_code == 0
    slides.assert_not_called()


def test_pending_review_has_nonzero_exit_and_regenerate_flag_is_forwarded(config_file):
    with patch("mynd.cli.ingest_slides", return_value=[{"status": "pending_review"}]) as slides:
        result = runner.invoke(app, ["ingest", "--kind", "slides", "--regenerate",
                                     "--config", str(config_file)])
    assert result.exit_code == 1
    assert "pending_review" in result.output
    assert slides.call_args.kwargs["regenerate"] is True


def test_invalid_kind_is_rejected(config_file):
    result = runner.invoke(app, ["ingest", "--kind", "invalid", "--config", str(config_file)])
    assert result.exit_code != 0
    assert "--kind" in result.output


def test_review_confirms_existing_course_and_shows_source_evidence(tmp_path):
    command = typer.Typer()
    course = Course("course-1", "Algorithms", tmp_path / "Algorithms" / "Course.md")
    proposal = CourseProposal("Binary search", course_id=course.id, language="English", order="2",
                              evidence=(Evidence("lecture-1", 1, "Algorithm Design and Analysis"),))
    assignments = []

    @command.command()
    def review():
        assignments.append(review_assignment(Path("02.pdf"), proposal, [course], tmp_path))

    result = runner.invoke(command, [], input="\n\n\n\n\n\n")
    assert result.exit_code == 0, result.output
    assert assignments[0].course == course
    assert assignments[0].order == "2"
    assert "Page 1: Algorithm Design and Analysis" in result.output


def test_review_new_course_accepts_unknown_optional_metadata(tmp_path):
    command = typer.Typer()
    assignments = []

    @command.command()
    def review():
        assignments.append(review_assignment(Path("01.pdf"),
                                             CourseProposal("Optimization", language="English"),
                                             [], tmp_path))

    result = runner.invoke(command, [], input="\n\nMachine Learning\n\n\n\n\n\n\n\n")
    assert result.exit_code == 0, result.output
    assert assignments[0].course.title == "Machine Learning"
    assert assignments[0].course.university == ""
    assert assignments[0].course.semester == ""
    assert assignments[0].order == "01"
