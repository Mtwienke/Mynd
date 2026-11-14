"""Plain terminal review; explicit confirmation before registering a topic."""
from __future__ import annotations

import typer

from mynd.ai.topic_policy import MINIMUM_SOURCES
from mynd.connect.models import Decision, Proposal


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
            if not topics:
                typer.echo("No approved topics exist yet.", err=True)
                continue
            for index, topic in enumerate(topics, 1):
                typer.echo(f"{index}. {topic['title']} ({topic['id']})", err=True)
            number = typer.prompt("Merge into topic number", type=int, err=True)
            if 1 <= number <= len(topics):
                return Decision("merge", topic_id=topics[number - 1]["id"])
        elif action in {"d", "adopt"}:
            if proposal.source_count < required:
                typer.echo("Adoption cannot bypass the source threshold.", err=True)
                continue
            path = typer.prompt("Existing note path relative to the vault", err=True)
            typer.echo("Its existing text stays intact; generated topic sections will be appended.", err=True)
            if typer.confirm("Adopt this note?", default=False, err=True):
                return Decision("approve", title=title, aliases=tuple(aliases), query=query,
                                adopt_path=path, definition=definition, breadth=breadth)
