"""Small, checksummed ownership markers; no inference or database access."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from markdown_it import MarkdownIt

from mynd.vault.safety import digest

_LINK = re.compile(
    r"<!-- mynd:link:([a-z0-9-]+):([0-9a-f]{64}):begin -->"
    r"(\[\[([^\]\n|]+)\|([^\]\n|]+)\]\])"
    r"<!-- mynd:link:\1:end -->"
)
_PARSER = MarkdownIt("commonmark")


@lru_cache(maxsize=32)
def code_ranges(content: str) -> tuple[tuple[int, int], ...]:
    offsets = [0]
    for line in content.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    result = []
    for token in _PARSER.parse(content):
        if token.type in {"fence", "code_block"} and token.map:
            result.append(tuple(offsets[min(line, len(offsets) - 1)] for line in token.map))
    result.extend((match.start(), match.end()) for match in re.finditer(
        r"(?<![\\`])(`+)(?!`)(.*?)(?<!`)\1(?!`)", content, re.DOTALL,
    ))
    return tuple(result)


def _outside_code(content: str, position: int) -> bool:
    return not any(start <= position < end for start, end in code_ranges(content))


@dataclass(frozen=True)
class OwnedLink:
    topic_id: str
    target: str
    label: str
    start: int
    end: int


def owned_links(content: str) -> list[OwnedLink]:
    if "<!-- mynd:link:" not in content:
        return []
    matches = [m for m in _LINK.finditer(content) if _outside_code(content, m.start())]
    count = sum(_outside_code(content, m.start())
                for m in re.finditer(r"<!-- mynd:link:", content))
    if count != 2 * len(matches):
        raise ValueError("Damaged Mynd inline-link markers; restore them before updating")
    result = []
    for match in matches:
        if digest(match[3]) != match[2]:
            raise ValueError("A Mynd inline link was edited; refusing to overwrite it")
        result.append(OwnedLink(match[1], match[4], match[5], match.start(), match.end()))
    return result


def unwrap_links(content: str) -> str:
    links = owned_links(content)
    for link in reversed(links):
        content = content[:link.start] + link.label + content[link.end:]
    return content


def wrap_link(topic_id: str, target: str, label: str) -> str:
    if not re.fullmatch(r"[a-z0-9-]+", topic_id):
        raise ValueError("Invalid topic ID")
    if any(char in target + label for char in "\r\n[]|"):
        raise ValueError("Unsafe inline-link text")
    link = f"[[{target}|{label}]]"
    return (f"<!-- mynd:link:{topic_id}:{digest(link)}:begin -->{link}"
            f"<!-- mynd:link:{topic_id}:end -->")


def block_range(content: str, key: str) -> tuple[int, int, str] | None:
    begin, end = f"<!-- mynd:{key}:begin -->", f"<!-- mynd:{key}:end -->"
    starts = [m.start() for m in re.finditer(re.escape(begin), content)
              if _outside_code(content, m.start())]
    stops = [m.start() for m in re.finditer(re.escape(end), content)
             if _outside_code(content, m.start())]
    if not starts and not stops:
        return None
    if len(starts) != 1 or len(stops) != 1:
        raise ValueError(f"Managed block '{key}' is missing/duplicated")
    start, stop = starts[0], stops[0]
    if stop < start:
        raise ValueError(f"Managed block '{key}' has reversed markers")
    inner = content[start + len(begin):stop]
    match = re.fullmatch(r"\n<!-- mynd:hash:([0-9a-f]{64}) -->\n(.*)\n", inner, re.DOTALL)
    if match is None or digest(match[2]) != match[1]:
        raise ValueError(f"Managed block '{key}' was edited or its checksum is missing")
    return start, stop + len(end), match[2]


def put_block(content: str, key: str, body: str) -> str:
    existing = block_range(content, key)
    block = (f"<!-- mynd:{key}:begin -->\n<!-- mynd:hash:{digest(body)} -->\n"
             f"{body}\n<!-- mynd:{key}:end -->")
    if existing:
        return content[:existing[0]] + block + content[existing[1]:]
    separator = "" if not content or content.endswith("\n\n") else "\n" if content.endswith("\n") else "\n\n"
    return content + separator + block + "\n"


def without_topics(content: str) -> str:
    existing = block_range(content, "topics")
    if existing:
        content = content[:existing[0]] + content[existing[1]:]
    return unwrap_links(content)


def validate_markers(content: str) -> None:
    stack = []
    seen = set()
    for match in re.finditer(r"<!-- mynd:([a-z][a-z0-9-]*):(begin|end) -->", content):
        if not _outside_code(content, match.start()):
            continue
        key, side = match[1], match[2]
        if side == "begin":
            if key in seen:
                raise ValueError(f"Duplicated managed block '{key}'")
            seen.add(key)
            stack.append(key)
        elif not stack or stack.pop() != key:
            raise ValueError(f"Damaged managed block '{key}'")
    if stack:
        raise ValueError(f"Unclosed managed block '{stack[-1]}'")
    owned_links(content)
    for key in ("topics", "topic-content"):
        block_range(content, key)
