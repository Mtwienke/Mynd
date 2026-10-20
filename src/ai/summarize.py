from __future__ import annotations

from dataclasses import dataclass

from ai.base import TextGenerator


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

    def summarize(self, text: str, metadata: dict | None = None) -> str:
        """Return a concise Markdown summary, grounded in the supplied paper text."""
        source = text.strip()
        if not source:
            raise ValueError("Cannot summarize empty paper text")

        context = self._metadata_context(metadata or {})
        chunks = self._chunks(source)
        if len(chunks) == 1:
            material = chunks[0]
        else:
            notes = [
                self.generator.generate(self._chunk_prompt(chunk, index, len(chunks), context))
                for index, chunk in enumerate(chunks, start=1)
            ]
            material = "\n\n".join(notes)

        prompt = f"""You are writing a study summary for a personal research library.
Use only claims supported by the source material below. Do not invent details.
If the text does not establish something, say so briefly. Write clear Markdown
under these headings: `## Overview`, `## Main contributions`, `## Approach`,
`## Findings`, and `## Limitations and open questions`. Omit a heading if the
source gives no useful information for it. Keep the whole summary concise.

Paper metadata:
{context or "Not provided"}

Source material:
{material}
"""
        return self.generator.generate(prompt).strip()

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
    def _metadata_context(metadata: dict) -> str:
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
