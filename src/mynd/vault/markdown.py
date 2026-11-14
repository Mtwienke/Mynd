"""Source-positioned Markdown inspection, not a Markdown round-trip formatter."""
from __future__ import annotations

import fnmatch
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import yaml
from markdown_it import MarkdownIt
from markdown_it.helpers import parseLinkDestination, parseLinkLabel, parseLinkTitle
from markdown_it.rules_inline import StateInline

from mynd.settings import Settings
from mynd.vault.managed import block_range, unwrap_links, validate_markers
from mynd.vault.safety import digest, read_text

PARSER_VERSION = "vault-markdown-v1"
_PARSER = MarkdownIt("commonmark").enable("table")
_WIKI = re.compile(r"(?<![\\\[])\[\[([^\]\n]+)\]\](?!\])")
_SKIP_SECTIONS = {
    "references", "bibliography", "extracted questions", "questions", "reading log",
    "provenance", "grobid dump", "extraction quality and provenance", "coverage", "lectures",
    "course revision quiz", "related topics", "related topics (mynd)",
}


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def path_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def markdown_links(content: str) -> tuple[list[tuple], list[tuple[int, int]]]:
    """Use the parser's balanced label/destination helpers, retaining exact spans."""
    state = StateInline(content, _PARSER, {}, [])
    found, damaged = [], []
    boundary = 0
    for match in re.finditer(r"(?<![\\\[])!?\[(?!\[)", content):
        if match.start() < boundary:
            continue
        start = match.start() + (1 if match[0].startswith("!") else 0)
        label_end = parseLinkLabel(state, start)
        if label_end < 0 or content[label_end + 1:label_end + 2] != "(":
            continue
        position = label_end + 2
        while position < len(content) and content[position].isspace():
            position += 1
        dest_start = position
        destination = parseLinkDestination(content, position, len(content))
        value = destination.str if destination.ok else ""
        if destination.ok:
            position = destination.pos
        dest_end = position
        after_destination = position
        while position < len(content) and content[position].isspace():
            position += 1
        if position > after_destination and content[position:position + 1] != ")":
            title = parseLinkTitle(content, position, len(content))
            if title.ok:
                position = title.pos
                while position < len(content) and content[position].isspace():
                    position += 1
        if content[position:position + 1] != ")":
            end = content.find("\n", label_end)
            damaged.append((match.start(), end if end >= 0 else len(content)))
            continue
        boundary = position + 1
        found.append((match.start(), boundary, content[start + 1:label_end], value,
                      dest_start, dest_end))
    return found, damaged


def blank(value: str) -> str:
    return "".join("\n" if char == "\n" else " " for char in value)


def mask_ranges(content: str, ranges: list[tuple[int, int]]) -> str:
    chars = list(content)
    for start, end in ranges:
        chars[start:end] = blank(content[start:end])
    return "".join(chars)


def plain(value: str) -> str:
    value = _WIKI.sub(lambda m: m[1].split("|", 1)[-1].split("/")[-1]
                      if "|" not in m[1] else m[1].split("|", 1)[1], value)
    links, _ = markdown_links(value)
    for start, end, label, _, _, _ in reversed(links):
        value = value[:start] + label + value[end:]
    return value.strip()


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, float, bool)) or key in result:
            raise ValueError("Duplicate or invalid YAML property")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True)
class Heading:
    start: int
    end: int
    level: int
    text: str


@dataclass(frozen=True)
class Link:
    start: int
    end: int
    target: str
    anchor: str = ""
    label: str = ""
    kind: str = "wiki"
    embedded: bool = False
    destination_start: int = 0
    destination_end: int = 0


@dataclass(frozen=True)
class Passage:
    id: str
    note_id: str
    path: str
    heading: str
    text: str
    start: int
    end: int


@dataclass
class Document:
    path: Path
    relative: str
    content: str
    metadata: dict = field(default_factory=dict)
    headings: list[Heading] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    protected: list[tuple[int, int]] = field(default_factory=list)
    evidence_protected: list[tuple[int, int]] = field(default_factory=list)
    inline_mask: str = ""
    table_ranges: list[tuple[int, int]] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return str(self.metadata.get("type", "note"))

    def error(self, rule: str, message: str, position: int = 0) -> None:
        self.errors.append({"rule": rule, "message": message,
                            "line": self.content.count("\n", 0, position) + 1})


def parse_document(path: Path, vault: Path, content: str | None = None) -> Document:
    content = read_text(path) if content is None else content
    doc = Document(path, path.relative_to(vault).as_posix(), content)
    offsets = [0]
    for line in content.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    front_end = 0
    if content.startswith(("---\n", "---\r\n")):
        closing = re.search(r"(?m)^(?:---|\.\.\.)\r?$", content[content.index("\n") + 1:])
        if closing is None:
            doc.error("frontmatter-unclosed", "YAML frontmatter is not closed")
            front_end = len(content)
        else:
            start = content.index("\n") + 1
            front_end = start + closing.end()
            try:
                metadata = yaml.load(content[start:start + closing.start()], Loader=_UniqueLoader)
                if metadata is not None and not isinstance(metadata, dict):
                    raise ValueError("Frontmatter must be a property mapping")
                doc.metadata = metadata or {}
            except (yaml.YAMLError, ValueError, TypeError) as exc:
                doc.error("frontmatter-invalid", str(exc))
        doc.protected.append((0, front_end))
    block_input = mask_ranges(content, doc.protected)
    tokens = _PARSER.parse(block_input)
    quotes = []
    tables = []
    for index, token in enumerate(tokens):
        if token.map is None:
            continue
        start, end = (offsets[min(line, len(offsets) - 1)] for line in token.map)
        if token.type in {"fence", "code_block", "html_block"}:
            # HTML comments include managed markers, validated separately below.
            doc.protected.append((start, end))
            if (token.type == "html_block" and content[start:end].lstrip().startswith("<!--")
                    and "-->" not in content[start:end]):
                doc.error("comment-unclosed", "HTML comment is not closed", start)
            if token.type == "fence":
                lines = content[start:end].rstrip("\r\n").splitlines()
                closing = re.compile(r"^ {0,3}" + re.escape(token.markup[0])
                                     + "{" + str(len(token.markup)) + r",}\s*$")
                if len(lines) < 2 or closing.fullmatch(lines[-1]) is None:
                    doc.error("fence-unclosed", "Code fence is not closed", start)
        elif token.type == "heading_open":
            inline = tokens[index + 1]
            text = "".join(child.content for child in inline.children or []
                           if child.type in {"text", "code_inline", "html_inline"})
            doc.headings.append(Heading(start, end, int(token.tag[1]), text))
        elif token.type == "blockquote_open":
            quotes.append((start, end))
        elif token.type == "table_open":
            tables.append((start, end))
    # Validate management syntax outside fences, but before hiding comments.
    code_ranges = []
    for token in tokens:
        if token.type in {"fence", "code_block"} and token.map:
            code_ranges.append(tuple(offsets[min(i, len(offsets) - 1)] for i in token.map))
    try:
        validate_markers(mask_ranges(content, code_ranges))
    except ValueError as exc:
        doc.error("managed-block-damaged", str(exc))
    doc.evidence_protected = list(doc.protected)
    for pattern, unclosed_rule in (
        (r"<!--.*?(?:-->|\Z)", "comment-unclosed"),
        (r"(?<!\\)%%.*?(?:%%|\Z)", "obsidian-comment-unclosed"),
        (r"(?ms)^ {0,3}\$\$.*?(?:\$\$|\Z)", "math-unclosed"),
    ):
        masked = mask_ranges(content, doc.protected)
        for match in re.finditer(pattern, masked, re.DOTALL):
            text = content[match.start():match.end()]
            ending = "-->" if text.startswith("<!--") else "%%" if "%%" in text else "$$"
            if not text.endswith(ending) or text.strip() == ending:
                doc.error(unclosed_rule, "Unclosed comment or math delimiter", match.start())
            doc.protected.append((match.start(), match.end()))
            if ending != "$$":
                doc.evidence_protected.append((match.start(), match.end()))
    masked = mask_ranges(content, doc.protected)
    for match in re.finditer(r"(?<![\\`])(`+)(?!`)(.*?)(?<!`)\1(?!`)", masked, re.DOTALL):
        doc.protected.append((match.start(), match.end()))
        doc.evidence_protected.append((match.start(), match.end()))
    masked = mask_ranges(content, doc.protected)
    for match in re.finditer(r"(?<![\\$])\$(?!\$)[^\n$]+?(?<!\\)\$(?!\$)", masked):
        doc.protected.append((match.start(), match.end()))
    masked = mask_ranges(content, doc.protected)
    md_links, damaged_links = markdown_links(masked)
    md_ranges = [(link[0], link[1]) for link in md_links]
    for match in _WIKI.finditer(mask_ranges(masked, md_ranges)):
        value = match[1].replace("\\|", "|")
        destination, _, label = value.partition("|")
        target, _, anchor = destination.partition("#")
        doc.links.append(Link(match.start(), match.end(), target, anchor, label,
                              embedded=match.start() > 0 and content[match.start() - 1] == "!"))
    for start, end, label, value, dest_start, dest_end in md_links:
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value) or value.startswith("//"):
            continue
        target, _, anchor = unquote(value).partition("#")
        doc.links.append(Link(start, end, target, anchor, label, "markdown", content[start] == "!",
                              dest_start, dest_end))
    for start, _ in damaged_links:
        doc.error("markdown-link-malformed", "Markdown link destination or closing parenthesis is malformed", start)
    wikiless = mask_ranges(masked, [(link.start, link.end) for link in doc.links] + md_ranges)
    for match in re.finditer(r"(?<!\\)\[\[|(?<!\\)\]\]", wikiless):
        doc.error("wikilink-malformed", "Unmatched wikilink brackets", match.start())
    for link in doc.links:
        if not link.target and not link.anchor:
            doc.error("wikilink-empty", "Empty internal link", link.start)
    inline_ranges = doc.protected + quotes + tables
    inline_ranges += [(h.start, h.end) for h in doc.headings]
    inline_ranges += [(link.start, link.end) for link in doc.links]
    # External/reference links and definitions are never inline-rewritten either.
    inline_ranges += [(link[0], link[1]) for link in md_links] + damaged_links
    inline_ranges += [(m.start(), m.end()) for m in re.finditer(
        r"!?\[[^\]\n]*\]\[[^\]\n]*\]|(?m:^ {0,3}\[[^\]\n]+\]:.*$)", masked)]
    inline_ranges += [(m.start(), m.end()) for m in re.finditer(
        r"<(?:https?://|mailto:)[^>\n]+>|https?://[^\s<>]+|(?m:(?:^|\s)\^[A-Za-z0-9-]+\s*$)", masked)]
    doc.inline_mask = mask_ranges(content, inline_ranges)
    doc.table_ranges = tables
    return doc


def passages(doc: Document, note_id: str, *, limit: int = 700) -> list[Passage]:
    """Stable evidence IDs use visible text, not generated links or line numbers."""
    content = unwrap_links(doc.content)
    source = parse_document(doc.path, doc.path.parent, content)
    topics = block_range(content, "topics")
    protected = source.evidence_protected + ([(topics[0], topics[1])] if topics else [])
    masked = mask_ranges(content, protected)
    headings = [h for h in source.headings if not topics or not topics[0] <= h.start < topics[1]]
    section = ""
    heading = ""
    current = 0
    result = []
    for h in [*headings, Heading(len(content), len(content), 1, "")]:
        if (current < h.start and section not in _SKIP_SECTIONS
                and not (doc.kind == "paper" and not section)):
            body = masked[current:h.start]
            for match in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", body):
                start = current + match.start()
                raw = match[0]
                if raw.lstrip().startswith((">", "**Sources:**", "- **", "_Add ", "_What ",
                                            "_Summary unavailable", "_No ", "_Study material")):
                    continue
                for part in re.finditer(r"[^\n]{1," + str(limit) + r"}(?:\s+|$)", raw):
                    text = plain(part[0])
                    if len(text.strip()) < 20:
                        continue
                    identifier = "src-" + digest(note_id + "\0" + heading + "\0" + text)[:24]
                    result.append(Passage(identifier, note_id, doc.relative, heading, text,
                                          start + part.start(), start + part.end()))
        if h.level <= 2:
            section = normalized(h.text) if h.level == 2 else ""
        heading = h.text
        current = h.end
    # Metadata/quality managed blocks have no enclosing heading in some templates.
    return [p for p in result if not any(
        doc.content.find(f"<!-- mynd:{key}:begin -->") >= 0
        and f"<!-- mynd:{key}:begin -->" in content[:p.start]
        and content.rfind(f"<!-- mynd:{key}:begin -->", 0, p.start)
        > content.rfind(f"<!-- mynd:{key}:end -->", 0, p.start)
        for key in ("metadata", "quality", "coverage", "lectures", "questions")
    )]


def vault_files(settings: Settings) -> list[Path]:
    vault = settings.vault_path
    if not vault.is_dir():
        raise ValueError(f"Vault directory does not exist: {vault}")
    result = []
    for path in sorted(vault.rglob("*")):
        relative = path.relative_to(vault)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if any(fnmatch.fnmatch(relative.as_posix(), pattern)
               for pattern in settings.maintenance_excludes):
            continue
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents
                                    if parent.is_relative_to(vault) and parent != vault):
            continue
        if path.is_file():
            result.append(path)
    return result


class LinkIndex:
    def __init__(self, vault: Path, files: list[Path]):
        self.vault = vault
        self.files = files
        self.relative = {path.relative_to(vault).as_posix(): path for path in files}

    def resolve(self, doc: Document, link: Link) -> list[Path]:
        if not link.target:
            return [doc.path]
        target = link.target.removeprefix("/")
        choices = {target, target + ".md"}
        if link.kind == "wiki" and "/" not in target:
            return [p for p in self.files if path_key(p.name) in {path_key(c) for c in choices}]
        if link.kind == "markdown":
            relative = (doc.path.parent / target).resolve()
            if relative.is_relative_to(self.vault.resolve()):
                rel = relative.relative_to(self.vault.resolve()).as_posix()
                choices.update((rel, rel + ".md"))
        exact = [p for key, p in self.relative.items() if key in choices]
        if exact:
            return exact
        folded = {path_key(choice) for choice in choices}
        if "/" in target:
            return [p for key, p in self.relative.items() if path_key(key) in folded]
        return [p for p in self.files if path_key(p.name) in folded]

    def fingerprint(self) -> str:
        return digest("\n".join(self.relative))
