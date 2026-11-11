from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from mynd.cli import app
from mynd.courses.models import Course, CourseProposal, Evidence
from mynd.courses.review import ReviewDraft, ReviewForm, review_assignment

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

    result = runner.invoke(command, [], input="save\ny\n")
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

    result = runner.invoke(command, [], input="1\nMachine Learning\nsave\ny\n")
    assert result.exit_code == 0, result.output
    assert assignments[0].course.title == "Machine Learning"
    assert assignments[0].course.university == ""
    assert assignments[0].course.semester == ""
    assert assignments[0].order == "01"


def _run_review(tmp_path, known, answers):
    command = typer.Typer()
    assignments = []

    @command.command()
    def review():
        assignments.append(review_assignment(
            Path("Algorithm_Chapter1-1.pdf"),
            CourseProposal("Algorithm Design and Analysis", language="English"), known, tmp_path,
        ))

    return runner.invoke(command, [], input=answers), assignments


def test_review_accepts_new_course_name_at_selection_prompt(tmp_path):
    result, assignments = _run_review(
        tmp_path, [], "course\nAlgorithm Design and Analysis\nsave\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert assignments[0].course.title == "Algorithm Design and Analysis"
    assert "Invalid course selection" not in result.output


def test_review_matches_existing_names_case_insensitively(tmp_path):
    course = Course("course-1", "Algorithm Design and Analysis", tmp_path / "Course.md")
    result, assignments = _run_review(
        tmp_path, [course], "course\n  algorithm  DESIGN and analysis  \nsave\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert assignments[0].course == course
    assert "course-1" in result.output


@pytest.mark.parametrize("invalid", ["0", "-1", "99", "   "])
def test_invalid_course_selection_reprompts_instead_of_aborting(tmp_path, invalid):
    course = Course("course-1", "Algorithms", tmp_path / "Course.md")
    result, assignments = _run_review(
        tmp_path, [course], f"course\n{invalid}\ncourse\n1\nsave\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert assignments[0].course == course
    assert result.output.count("Course number or name") == 2


def test_ambiguous_course_name_requires_an_explicit_selection(tmp_path):
    courses = [Course(f"course-{index}", "Algorithms", tmp_path / str(index) / "Course.md",
                      semester=str(index)) for index in range(2)]
    result, assignments = _run_review(tmp_path, courses,
                                      "course\nAlgorithms\ncourse\n2\nsave\ny\n")
    assert result.exit_code == 0, result.output
    assert "Several courses have that name" in result.output
    assert assignments[0].course == courses[1]


def test_whitespace_course_name_reprompts(tmp_path):
    result, assignments = _run_review(
        tmp_path, [], "1\n   \nsave\n1\nAlgorithms\nsave\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert "must not be empty" in result.output
    assert assignments[0].course.title == "Algorithms"


def test_declining_confirmation_returns_to_editing_and_keeps_draft(tmp_path):
    result, assignments = _run_review(tmp_path, [],
        "1\nWrong name\n7\nChinese\nsave\nn\n1\nAlgorithms\n7\nEnglish\nsave\ny\n")
    assert result.exit_code == 0, result.output
    assert assignments[0].course.title == "Algorithms"
    assert assignments[0].language == "English"
    assert result.output.count("Save this assignment?") == 2


def test_skip_is_explicit_and_cancel_aborts(tmp_path):
    result, assignments = _run_review(tmp_path, [], "skip\n")
    assert result.exit_code == 0
    assert assignments == [None]
    result, assignments = _run_review(tmp_path, [], "cancel\n")
    assert result.exit_code != 0
    assert not assignments


def test_editing_existing_course_preserves_id_path_and_generated_data_in_draft(tmp_path):
    course = Course("course-1", "Typo", tmp_path / "Typo" / "Course.md", semester="2026")
    draft = ReviewDraft(CourseProposal("Lecture", course_id=course.id), [course], tmp_path,
                        Path("01.pdf"))
    draft.values["course_title"] = "Corrected"
    edited = draft.assignment().course
    assert edited.id == course.id and edited.note_path == course.note_path
    assert edited.title == "Corrected" and edited.semester == "2026"
    assert course.title == "Typo"  # Editing a draft never mutates persisted models.


def test_switching_courses_prefills_shared_fields_but_keeps_lecture_edits(tmp_path):
    course = Course("course-1", "Algorithms", tmp_path / "Course.md", university="University",
                    semester="2026", lecturer="Professor", language="English")
    draft = ReviewDraft(CourseProposal("Lecture"), [course], tmp_path, Path("01.pdf"))
    draft.values["title"] = "Edited lecture"
    draft.values["language"] = "German"
    draft.select(course)
    assert draft.values["university"] == "University"
    assert draft.values["course_language"] == "English"
    assert draft.assignment().title == "Edited lecture"
    assert draft.assignment().language == "German"


def test_saved_blank_order_remains_blank_instead_of_reverting_to_filename(tmp_path):
    draft = ReviewDraft(CourseProposal("Lecture", course_title="Algorithms", saved=True),
                        [], tmp_path, Path("archived-source-with-hash.pdf"))
    assert draft.values["order"] == ""


def test_fullscreen_form_edits_saved_language_and_confirms_with_keyboard(tmp_path):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    draft = ReviewDraft(CourseProposal("Lecture", course_title="Algorithms", language="Chinese"),
                        [], tmp_path, Path("01.pdf"))
    with create_pipe_input() as pipe:
        form = ReviewForm(draft, input=pipe, output=DummyOutput())
        form.app.layout.focus(form.entries["language"])
        pipe.send_text("\x01\x0bEnglish\x13\r")
        assignment = form.run()
    assert assignment.language == "English"
    assert assignment.course.title == "Algorithms"


def test_fullscreen_decline_reopens_same_draft(tmp_path):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    draft = ReviewDraft(CourseProposal("Lecture", course_title="Typo"), [], tmp_path,
                        Path("01.pdf"))
    with create_pipe_input() as pipe:
        form = ReviewForm(draft, input=pipe, output=DummyOutput())
        pipe.send_text("\x13\t\r\x01\x0bAlgorithms\x13\r")
        assignment = form.run()
    assert assignment.course.title == "Algorithms"


def test_review_flag_rejects_noninteractive_before_starting_ingestion(config_file):
    with patch("mynd.cli.ingest_slides") as slides:
        result = runner.invoke(app, ["ingest", "--review", "--non-interactive",
                                     "--config", str(config_file)])
    assert result.exit_code != 0
    assert "interactive terminal" in result.output
    slides.assert_not_called()


def test_review_flag_is_forwarded(config_file):
    with (patch("mynd.cli.sys") as terminal,
          patch("mynd.cli.ingest_slides", return_value=[{"status": "skipped"}]) as slides):
        terminal.stdin.isatty.return_value = True
        result = runner.invoke(app, ["ingest", "--kind", "slides", "--review",
                                     "--plain-review", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    assert slides.call_args.kwargs["review_existing"] is True
    assert callable(slides.call_args.args[1])


@pytest.mark.parametrize("rows, columns", [(24, 80), (40, 120), (16, 60)])
def test_fullscreen_resize_and_tab_navigation_reach_all_fields(tmp_path, rows, columns):
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    class Output(DummyOutput):
        def get_size(self):
            return Size(rows=rows, columns=columns)

    with create_pipe_input() as pipe:
        form = ReviewForm(ReviewDraft(CourseProposal("Lecture", course_title="Algorithms"),
                                      [], tmp_path, Path("01.pdf")), input=pipe, output=Output())
        rendered = []

        def after_render(_):
            if len(rendered) >= 2:
                return
            screen = form.app.renderer.last_rendered_screen
            rendered.append("".join(cell.char for row in screen.data_buffer.values()
                                     for cell in row.values()))
            if len(rendered) == 1:
                # From Course name, seven Tabs must reach Lecture order even off-screen.
                pipe.send_text("\t" * 7 + "\x01\x0bEdited order\x13")
            elif len(rendered) == 2:
                pipe.send_text("\r")

        form.app.after_render += after_render
        assignment = form.run()
    assert assignment.order == "Edited order"
    assert all("Window too small" not in frame for frame in rendered)
    assert "Course name" in rendered[0]
    assert "Save this assignment?" in rendered[1]


def test_fullscreen_course_picker_prefills_existing_course_and_renaming_keeps_id(tmp_path):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    course = Course("course-1", "Algorithms", tmp_path / "Course.md", university="Xidian")
    draft = ReviewDraft(CourseProposal("Lecture"), [course], tmp_path, Path("01.pdf"))
    with create_pipe_input() as pipe:
        form = ReviewForm(draft, input=pipe, output=DummyOutput())
        form.app.layout.focus(form.course_button)
        # Open picker, select the existing course, Use, rename its prefilled title, save.
        pipe.send_text("\r\x1b[B \t\r\x01\x0bRenamed\x13\r")
        assignment = form.run()
    assert assignment.course.id == course.id
    assert assignment.course.note_path == course.note_path
    assert assignment.course.title == "Renamed"
    assert assignment.course.university == "Xidian"


def test_fullscreen_skip_and_cancel_do_not_return_assignments(tmp_path):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    def form(pipe):
        return ReviewForm(ReviewDraft(CourseProposal("Lecture", course_title="Algorithms"),
                                      [], tmp_path, Path("01.pdf")),
                          input=pipe, output=DummyOutput())

    with create_pipe_input() as pipe:
        review = form(pipe)
        # From Course name: seven remaining fields, evidence, Save, then Skip.
        pipe.send_text("\t" * 10 + "\r")
        assert review.run() is None
    with create_pipe_input() as pipe:
        pipe.send_text("\x03")
        with pytest.raises(KeyboardInterrupt):
            form(pipe).run()


def test_plain_form_can_clear_optional_fields(tmp_path):
    command = typer.Typer()
    assignments = []

    @command.command()
    def review():
        assignments.append(review_assignment(Path("01.pdf"), CourseProposal(
            "Lecture", course_title="Algorithms", university="Incorrect university"),
            [], tmp_path, plain=True))

    result = runner.invoke(command, [], input="2\n\nsave\ny\n")
    assert result.exit_code == 0, result.output
    assert assignments[0].course.university == ""


def test_metadata_review_command_does_not_start_ingestion_or_model(config_file):
    with (patch("mynd.cli.review_saved_slides", return_value=[
              {"status": "metadata_updated", "lecture_id": "lecture-1"},
          ]) as review,
          patch("mynd.cli.ingest_slides") as ingest,
          patch("mynd.cli.build_text_generator") as generator):
        result = runner.invoke(app, ["review-slides", "lecture-1", "--plain-review",
                                     "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    assert review.call_args.args[2] == "lecture-1"
    assert "Metadata saved" in result.stderr
    assert '"status": "metadata_updated"' in result.stdout
    ingest.assert_not_called()
    generator.assert_not_called()
