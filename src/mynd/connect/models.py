from __future__ import annotations

from dataclasses import dataclass

from mynd.vault.markdown import Passage


@dataclass(frozen=True)
class Proposal:
    id: str
    title: str
    definition: str
    aliases: tuple[str, ...]
    evidence: tuple[Passage, ...]
    important: bool
    rationale: str
    possible_matches: tuple[str, ...] = ()
    grouping_warning: str = ""
    breadth: str = "umbrella"
    source_count: int = 0
    minimum_sources: int = 1
    candidate_id: str = ""


@dataclass(frozen=True)
class Decision:
    action: str  # approve, merge, reject, defer
    title: str = ""
    topic_id: str = ""
    aliases: tuple[str, ...] | None = None
    query: str = ""
    adopt_path: str = ""
    definition: str = ""
    breadth: str = ""
