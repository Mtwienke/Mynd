from __future__ import annotations

import json
import re

from mynd.vault.connections import inline_label
from mynd.vault.managed import block_range, put_block
from mynd.vault.markdown import Passage

_STALE_NOTICE = ("_Topic update pending: supporting notes or generation may be incomplete. "
                 "Existing content is retained; rerun `mynd connect`._")


def mark_topic_stale(content: str) -> str:
    """Only touch an intact owned block; handwritten/edited sections stay untouched."""
    block = block_range(content, "topic-content")
    if block is None or _STALE_NOTICE in block[2]:
        return content
    return put_block(content, "topic-content", _STALE_NOTICE + "\n\n" + block[2])


def _prose(value: str) -> str:
    # All destinations and management syntax are rendered by code, never the model.
    value = " ".join(value.split()).replace("<", "&lt;").replace("[", "\\[")
    value = re.sub(r"^\s*#{1,6}\s*", "", value).strip()
    return re.sub(r"^[`~]{3,}", lambda match: "\\" + match[0], value)


def render_topic(topic: dict, previous: str | None, sources: dict[str, Passage],
                 mentions: list[dict], paragraphs: list[dict], web: dict | None,
                 web_paragraphs: list[dict], *, web_error: str = "", model: str = "") -> str:
    content = previous or (
        f"---\ntype: topic\nwork_id: {json.dumps(topic['id'])}\n"
        f"aliases: {json.dumps(topic['aliases'], ensure_ascii=False)}\n---\n\n"
        f"# {inline_label(topic['title'])}\n\n"
        "## My notes\n\n_Add personal notes here; this area is never regenerated._\n"
    )
    if previous and not topic.get("adopted"):
        # Update our exact generated alias line only. Hand-formatted/edited YAML
        # remains user-owned, including comments, extra properties, and aliases.
        old_aliases = topic.get("previous_aliases", topic["aliases"])
        old_line = "aliases: " + json.dumps(old_aliases, ensure_ascii=False)
        new_line = "aliases: " + json.dumps(topic["aliases"], ensure_ascii=False)
        front_end = content.find("\n---", 4) if content.startswith("---\n") else -1
        if front_end >= 0:
            prefix = re.sub(r"(?m)^" + re.escape(old_line) + "$", lambda _: new_line, content[:front_end])
            content = prefix + content[front_end:]
    # Adoption is explicit, and wraps only a newly appended section; handwritten
    # frontmatter, headings, and text are never relabeled or replaced.
    if previous and not topic.get("adopted") and block_range(previous, "topic-content") is None:
        raise ValueError("Existing topic file has no ownership block; explicitly adopt it in review")
    lines = ["## From your vault — AI-generated synthesis", "",
             "_Based on Markdown notes, not independently verified against original documents._", ""]
    for paragraph in paragraphs:
        citations = []
        for identifier in paragraph["sources"]:
            source = sources[identifier]
            path = source.path.removesuffix(".md")
            label = inline_label(source.path.rsplit("/", 1)[-1].removesuffix(".md"))
            # Repeated headings would produce ambiguous anchors; note-level links
            # remain valid and exact excerpts are retained in the evidence section.
            heading = source.heading if source.heading and not any(
                char in source.heading for char in "[]|#\r\n"
            ) else ""
            citations.append(f"[[{path}{'#' + heading if heading else ''}|{label}]]")
        lines.extend([_prose(paragraph["text"]) + " " + " ".join(dict.fromkeys(citations)), ""])
    if not paragraphs:
        lines.extend(["_No substantive supporting passages remain; previous claims were withdrawn._", ""])
    lines.extend(["## Web overview — AI generated", ""])
    if web and web_paragraphs:
        web_sources = {item["id"]: item for item in web["sources"]}
        for paragraph in web_paragraphs:
            refs = []
            for identifier in paragraph["sources"]:
                source = web_sources[identifier]
                title = inline_label(source["title"]).replace("<", "&lt;")
                refs.append(f"[{title}](<{source['url']}>)")
            lines.extend([_prose(paragraph["text"]) + " " + " ".join(dict.fromkeys(refs)), ""])
        lines.extend(["Retrieved: " + web["fetched_at"] + ".", ""])
        if web_error:
            lines.extend(["_" + _prose(web_error) + " Previous web content is retained._", ""])
    else:
        lines.extend(["_" + _prose(web_error or "Web enrichment disabled; use --web to enable it.") + "_", ""])
    lines.extend(["## Mentions in this vault", ""])
    paths = sorted({sources[item["source"]].path for item in mentions})
    lines.extend(f"- [[{path.removesuffix('.md')}]]" for path in paths)
    if not paths:
        lines.append("_No current mentions. This topic is retained for your personal notes._")
    lines.extend(["", "## Supporting excerpts", ""])
    cited = {identifier for paragraph in paragraphs for identifier in paragraph["sources"]}
    for identifier in sorted(cited):
        source = sources[identifier]
        lines.extend([f"- [[{source.path.removesuffix('.md')}]]: “{_prose(source.text)}”", ""])
    lines.extend(["## Generation provenance", "",
                  f"- Local model: `{inline_label(model)}`.",
                  "- Citation validation verifies provenance, not factual correctness or entailment.",
                  "- Topic: " + _prose(topic["title"]) + ". Aliases: "
                  + ", ".join(_prose(alias) for alias in topic["aliases"]) + "."])
    return put_block(content, "topic-content", "\n".join(lines))
