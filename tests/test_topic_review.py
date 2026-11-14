from dataclasses import replace

import pytest

from mynd.connect.models import Proposal
from mynd.connect.review import review_topic


@pytest.fixture
def proposal():
    return Proposal("proposal-example", "Example concept", "A meaningful concept.", ("Example",), (),
                    False, "A broad topic.", breadth="umbrella", source_count=5, minimum_sources=1)


@pytest.fixture
def topics():
    return [{"id": "topic-" + breadth, "title": title, "definition": definition, "breadth": breadth}
            for breadth, title, definition in [
                ("umbrella", "Artificial Intelligence", "Broad study of intelligent computational systems."),
                ("established", "Reinforcement Learning", "Learning through interaction and rewards."),
                ("specialized", "LoRA", "Low-rank adaptation of model parameters."),
                ("niche", "A paper-specific variant", "A narrowly defined research invention."),
            ]]


def answer(monkeypatch, values):
    values = iter(values)
    def prompt(*args, **kwargs):
        assert kwargs.get("type") is not int, "Merge navigation must accept exit/text choices"
        value = next(values)
        return value or kwargs.get("default", "")
    monkeypatch.setattr("mynd.connect.review.typer.prompt", prompt)


@pytest.mark.parametrize("exit_choice", ["exit", "x", "ext", "cancel", "back", "q", "quit", "0", ""])
def test_merge_exit_returns_to_review_without_merging_or_quitting(proposal, topics, monkeypatch, exit_choice, capsys):
    answer(monkeypatch, ["m", exit_choice, "s"])
    result = review_topic(proposal, topics)
    assert result.action == "defer" and not result.topic_id
    assert "Merge cancelled; no topic selected" in capsys.readouterr().err


@pytest.mark.parametrize(("choice", "breadth"), [
    ("1", "umbrella"), ("2", "established"), ("3", "specialized"), ("4", "niche"),
    ("umbrella", "umbrella"), ("established", "established"),
    ("specialized", "specialized"), ("NICHE", "niche"),
])
def test_merging_can_select_every_breadth_by_name_or_number(proposal, topics, monkeypatch, choice, breadth, capsys):
    answer(monkeypatch, ["m", "other breadth", choice, "1"])
    result = review_topic(proposal, topics)
    assert result.action == "merge" and result.topic_id == "topic-" + breadth
    assert result.breadth == ""  # Browsing target categories must not reclassify the proposal.
    output = capsys.readouterr().err
    assert "Which breadth" in output and f"Merge targets — {breadth}" in output
    assert f"{breadth} (1 approved topic(s))" in output


def test_breadth_chooser_also_has_a_cancel_path(proposal, topics, monkeypatch):
    answer(monkeypatch, ["m", "o", "exit", "r"])
    result = review_topic(proposal, topics)
    assert result.action == "reject" and not result.topic_id


def test_picker_allows_repeated_breadth_changes_before_selecting(proposal, topics, monkeypatch):
    answer(monkeypatch, ["m", "o", "2", "o", "3", "o", "4", "o", "1", "1"])
    assert review_topic(proposal, topics).topic_id == "topic-umbrella"


def test_empty_breadth_does_not_trap_user_or_reuse_previous_topic_numbers(proposal, topics, monkeypatch, capsys):
    topics = [topic for topic in topics if topic["breadth"] != "specialized"]
    answer(monkeypatch, ["m", "o", "specialized", "1", "o", "established", "1"])
    assert review_topic(proposal, topics).topic_id == "topic-established"
    output = capsys.readouterr().err
    assert "specialized (0 approved topic(s))" in output
    assert "No approved topics at this breadth" in output


def test_no_merge_targets_still_allows_browsing_and_exit(proposal, monkeypatch):
    answer(monkeypatch, ["m", "o", "niche", "exit", "s"])
    assert review_topic(proposal, []).action == "defer"


def test_invalid_choices_reprompt_and_cancel_does_not_change_proposal_breadth(proposal, topics, monkeypatch, capsys):
    answer(monkeypatch, ["m", "banana", "999", "-1", "o", "99", "unknown", "established", "0", "a"])
    result = review_topic(proposal, topics)
    assert result.action == "approve" and result.breadth == "umbrella" and not result.topic_id
    assert "Choose a listed topic number" in capsys.readouterr().err


def test_initial_scope_uses_current_review_breadth_and_exit_preserves_edits(proposal, topics, monkeypatch, capsys):
    answer(monkeypatch, ["b", "specialized", "m", "exit", "a"])
    result = review_topic(proposal, topics)
    assert result.action == "approve" and result.breadth == "specialized"
    assert "Merge targets — specialized" in capsys.readouterr().err


def test_cancel_preserves_title_definition_aliases_and_query_edits(proposal, topics, monkeypatch):
    answer(monkeypatch, ["e", "Edited title", "Edited definition", "Alias one; Alias two", "Edited query",
                         "m", "exit", "a"])
    result = review_topic(proposal, topics)
    assert result.action == "approve" and result.title == "Edited title"
    assert result.definition == "Edited definition" and result.aliases == ("Alias one", "Alias two")
    assert result.query == "Edited query"


def test_target_numbers_are_stable_and_sorted_within_the_selected_breadth(proposal, topics, monkeypatch, capsys):
    topics = [dict(topics[0], id="topic-z", title="Z topic"), dict(topics[0], id="topic-a", title="A topic")]
    answer(monkeypatch, ["m", "1"])
    assert review_topic(proposal, topics).topic_id == "topic-a"
    assert "1. A topic (topic-a)" in capsys.readouterr().err


def test_cancel_can_be_followed_by_an_explicit_quit_from_main_review(proposal, topics, monkeypatch):
    import typer
    answer(monkeypatch, ["m", "exit", "q"])
    with pytest.raises(typer.Abort):
        review_topic(proposal, topics)


def test_merge_browsing_does_not_bypass_approval_threshold(proposal, topics, monkeypatch, capsys):
    proposal = replace(proposal, breadth="niche", source_count=1, minimum_sources=5)
    answer(monkeypatch, ["m", "o", "umbrella", "exit", "a", "s"])
    assert review_topic(proposal, topics).action == "defer"
    assert "Below the hard source threshold" in capsys.readouterr().err
