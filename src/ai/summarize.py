from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from ai.base import TextGenerator
from mynd.contracts import CitationRecord, PaperMetadata

logger = logging.getLogger(__name__)


@dataclass
class SummaryWriter:
    """Create a grounded, Markdown summary of a paper using a text model.

    Long papers are summarized in overlapping text chunks, then condensed into
    one final summary. File access and model connectivity belong to the injected
    generator, keeping this class easy to use with local or hosted backends.
    """

    generator: TextGenerator
    chunk_chars: int = 12_000
    overlap_chars: int = 500

    def __post_init__(self) -> None:
        if self.chunk_chars < 1:
            raise ValueError("chunk_chars must be positive")
        if self.overlap_chars < 0 or self.overlap_chars >= self.chunk_chars:
            raise ValueError("overlap_chars must be between 0 and chunk_chars")

    def summarize(
        self,
        text: str,
        metadata: PaperMetadata | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> str:
        """Return a concise Markdown summary, grounded in the supplied paper text."""
        report = progress or (lambda _message: None)
        source = text.strip()
        if not source:
            raise ValueError("Cannot summarize empty paper text")

        context = self._metadata_context(metadata or {})
        chunks = self._chunks(source)
        if len(chunks) == 1:
            report("Summarizing text part 1/1")
            material = chunks[0]
        else:
            notes = []
            for index, chunk in enumerate(chunks, start=1):
                report(f"Summarizing text part {index}/{len(chunks)}")
                notes.append(self.generator.generate(
                    self._chunk_prompt(chunk, index, len(chunks), context)
                ))
            material = "\n\n".join(notes)

        report("Generating final paper summary")
        prompt = f"""You are writing a study summary for a personal research library.
Use only claims supported by the source material below. Do not invent details.
If the text does not establish something, say so briefly. Output clear Markdown
using these exact headings in this exact order: `## Overview`,
`## Main contributions`, `## Approach`, `## Findings`, and
`## Limitations and open questions`. Do not rename or omit these headings.
If a heading has little evidence, say so briefly under it. Summarize the paper's
own claims and findings. Do not add citation callouts, bibliography entries, or
claims about cited works.
Keep the whole summary concise.

Paper metadata:
{context or "Not provided"}

Source material:
{material}
"""
        result = self.generator.generate(prompt).strip()
        return result

    def annotate_references(
        self,
        text: str,
        references: list[CitationRecord],
        metadata: PaperMetadata | None = None,
    ) -> list[CitationRecord]:
        """Add tentative reading cues to extracted references, grounded in the paper text."""
        if not references:
            return references
        context = self._metadata_context(metadata or {})
        entries = "\n".join(f"{i}: {ref.get('raw_reference', '')}" for i, ref in enumerate(references))
        prompt = f"""For each bibliography entry, classify its apparent role in the source paper using only evidence in the paper text. Return JSON only: an array of objects with index, importance, similarity, and directions. importance must be one of foundational, closely related, supporting, unclear. similarity must be one of very similar, related, adjacent, unclear. directions must be a short array chosen from method, benchmark, topic, dataset, theory, context, comparison. Use unclear when evidence is insufficient. Do not infer importance merely from citation presence.
Paper metadata:\n{context or 'Not provided'}\n\nBibliography entries:\n{entries}\n\nPaper text (bibliography excluded):\n{text[:50000]}"""
        try:
            result = self.generator.generate(prompt).strip()
            result = result.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            annotations = json.loads(result)
        except json.JSONDecodeError as exc:
            # Only malformed optional annotation output is recoverable; model and
            # network failures propagate to the ingestion failure boundary.
            logger.warning("Skipping reference annotations; model returned invalid JSON: %s", exc)
            return references
        if not isinstance(annotations, list):
            logger.warning("Skipping reference annotations; model returned JSON that is not a list")
            return references
        for item in annotations:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            if isinstance(index, int) and 0 <= index < len(references):
                ref = references[index]
                ref["importance"] = item.get("importance")
                ref["similarity"] = item.get("similarity")
                directions = item.get("directions")
                ref["direction"] = (
                    ", ".join(directions) if isinstance(directions, list) else None
                )
        return references

    def _chunks(self, text: str) -> list[str]:
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + self.chunk_chars, len(text))
            if end < len(text):
                # Prefer a paragraph boundary, avoiding tiny fragments.
                boundary = text.rfind("\n\n", start + self.chunk_chars // 2, end)
                if boundary > start:
                    end = boundary
            chunks.append(text[start:end].strip())
            if end == len(text):
                break
            start = max(end - self.overlap_chars, start + 1)
        return [chunk for chunk in chunks if chunk]

    @staticmethod
    def _metadata_context(metadata: PaperMetadata) -> str:
        fields = ("title", "authors", "year", "venue", "doi", "abstract")
        return "\n".join(
            f"{key}: {value}" for key in fields
            if (value := metadata.get(key)) not in (None, "", [])
        )

    @staticmethod
    def _chunk_prompt(chunk: str, index: int, count: int, context: str) -> str:
        return f"""Extract the key claims, methods, evidence, caveats, and open questions
from this part of a research paper. Use only information in this text. These are
intermediate notes for a later summary, so retain useful specifics without
adding conclusions. This is part {index} of {count}.

Paper metadata:
{context or "Not provided"}

Text:
{chunk}
"""
