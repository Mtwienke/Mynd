"""Plain terminal review; explicit confirmation before registering a topic."""
from __future__ import annotations

import typer

from mynd.ai.topic_policy import MINIMUM_SOURCES
from mynd.connect.models import Decision, Proposal
from mynd.connect.progress import label

_EXIT_MERGE = {"x", "exit", "ext", "cancel", "back", "q", "quit", "0"}
_OTHER_BREADTH = {"o", "other", "other breadth", "other-breadth", "breadth", "b"}


def _choose_merge_breadth(topics: list[dict]) -> str | None:
    breadths = list(MINIMUM_SOURCES)
    while True:
        typer.echo("\nWhich breadth should the merge target have?", err=True)
        for number, breadth in enumerate(breadths, 1):
            count = sum(topic.get("breadth") == breadth for topic in topics)
            typer.echo(f"{number}. {breadth} ({count} approved topic(s))", err=True)
        typer.echo("[x] Exit merge — return to the proposal without merging", err=True)
        choice = typer.prompt("Breadth name/number, or exit", default="x", err=True).strip().lower()
        if choice in _EXIT_MERGE:
            return None
        if choice in breadths:
            return choice
        if choice in {str(number) for number in range(1, len(breadths) + 1)}:
            return breadths[int(choice) - 1]
        typer.echo("Choose a listed breadth name/number, or 'exit'.", err=True)


def _choose_merge_target(topics: list[dict], breadth: str) -> str | None:
    """Browsing never changes the proposal's breadth or approves a new target."""
    typer.echo("Merge targets are policy-approved topics; candidates awaiting approval are not included.", err=True)
    while True:
        targets = sorted((topic for topic in topics if topic.get("breadth") == breadth),
                         key=lambda topic: (topic["title"].casefold(), topic["id"]))
        typer.echo(f"\nMerge targets — {breadth} ({len(targets)} approved topic(s))", err=True)
        for number, topic in enumerate(targets, 1):
            typer.echo(f"{number}. {label(topic['title'])} ({topic['id']})", err=True)
            if topic.get("definition"):
                typer.echo("   " + label(topic["definition"], 160), err=True)
        if not targets:
            typer.echo("No approved topics at this breadth. Choose another breadth or exit merge.", err=True)
        typer.echo("[o] Other breadth\n[x] Exit merge — return to the proposal without merging", err=True)
        choice = typer.prompt("Topic number, other breadth, or exit", default="x", err=True).strip().lower()
        if choice in _EXIT_MERGE:
            return None
        if choice in _OTHER_BREADTH:
            selected = _choose_merge_breadth(topics)
            if selected is None:
                return None
            breadth = selected
        elif choice in {str(number) for number in range(1, len(targets) + 1)}:
            return targets[int(choice) - 1]["id"]
        else:
            typer.echo("Choose a listed topic number, 'other breadth', or 'exit'.", err=True)


def review_topic(proposal: Proposal, topics: list[dict]) -> Decision:
    title = proposal.title
    definition = proposal.definition
    aliases = list(proposal.aliases)
    breadth = proposal.breadth
    query = title + " definition overview"
    typer.echo(f"\nTopic proposal: {title}", err=True)
    typer.echo(proposal.definition, err=True)
    if proposal.grouping_warning:
        typer.echo("Warning: " + proposal.grouping_warning, err=True)
    typer.echo(f"Candidate: {proposal.candidate_id}\n"
               f"Support: {proposal.source_count} original source(s). Aliases: {'; '.join(aliases) or 'none'}", err=True)
    typer.echo("Breadth rationale: " + proposal.rationale, err=True)
    for source in proposal.evidence:
        typer.echo(f"  {source.path} / {source.heading or '(body)'}\n    {source.text}", err=True)
    if proposal.possible_matches:
        typer.echo("Possible existing matches: " + "; ".join(proposal.possible_matches), err=True)
    while True:
        required = MINIMUM_SOURCES[breadth]
        typer.echo(f"Title: {title}\nBreadth: {breadth} ({proposal.source_count}/{required} original sources)\n"
                   f"Aliases: {'; '.join(aliases)}\nWeb query: {query}", err=True)
        action = typer.prompt(
            "[a]pprove, [e]dit, [b]readth, [m]erge, a[d]opt existing note, [r]eject, [s]kip, [q]uit",
            default="s", err=True,
        ).strip().lower()
        if action in {"a", "approve"}:
            if proposal.source_count < required:
                typer.echo("Below the hard source threshold; correct the category or defer this candidate.", err=True)
                continue
            return Decision("approve", title=title, aliases=tuple(aliases), query=query,
                            definition=definition, breadth=breadth)
        if action in {"r", "reject"}:
            return Decision("reject")
        if action in {"s", "skip", "defer"}:
            return Decision("defer", breadth=breadth)
        if action in {"q", "quit", "cancel"}:
            raise typer.Abort()
        if action in {"e", "edit"}:
            title = typer.prompt("English title", default=title, err=True)
            definition = typer.prompt("Concept/sense definition", default=definition, err=True)
            aliases = [item.strip() for item in typer.prompt(
                "Aliases (semicolon-separated)", default="; ".join(aliases), err=True,
            ).split(";") if item.strip()]
            query = typer.prompt("Public web query (no private note content)", default=query, err=True)
        elif action in {"b", "breadth"}:
            choice = typer.prompt("Breadth: umbrella / established / specialized / niche", default=breadth, err=True)
            if choice in MINIMUM_SOURCES:
                breadth = choice
            else:
                typer.echo("Unknown breadth category.", err=True)
        elif action in {"m", "merge"}:
            target = _choose_merge_target(topics, breadth)
            if target is not None:
                return Decision("merge", topic_id=target)
            typer.echo("Merge cancelled; no topic selected. Returning to proposal review.", err=True)
        elif action in {"d", "adopt"}:
            if proposal.source_count < required:
                typer.echo("Adoption cannot bypass the source threshold.", err=True)
                continue
            path = typer.prompt("Existing note path relative to the vault", err=True)
            typer.echo("Its existing text stays intact; generated topic sections will be appended.", err=True)
            if typer.confirm("Adopt this note?", default=False, err=True):
                return Decision("approve", title=title, aliases=tuple(aliases), query=query,
                                adopt_path=path, definition=definition, breadth=breadth)
