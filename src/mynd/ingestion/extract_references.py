import re
import time
from pathlib import Path
from typing import Any

import httpx
from defusedxml import ElementTree as ET

from mynd.contracts import GrobidReference, ReferenceRecord
from mynd.resolution.doi import normalize_doi

TEI_NAMESPACE = "http://www.tei-c.org/ns/1.0"
_NAMESPACE = {"tei": TEI_NAMESPACE}
_YEAR = re.compile(r"\b(\d{4})\b")
_SCOPE_NAMES = {"vol": "volume", "volume": "volume", "issue": "issue", "page": "pages"}
XmlNode = Any


def extract_grobid_references(
    pdf_path: Path,
    base_url: str,
    timeout: float = 90.0,
) -> list[ReferenceRecord]:
    """Extract bibliography records from a PDF using a running GROBID service."""
    response = None
    for attempt in range(3):
        try:
            with pdf_path.open("rb") as handle:
                response = httpx.post(
                    f"{base_url.rstrip('/')}/api/processReferences",
                    files={"input": (pdf_path.name, handle, "application/pdf")},
                    data={"consolidateCitations": "0"},
                    timeout=timeout,
                )
            response.raise_for_status()
            break
        except (httpx.ConnectError, httpx.ReadTimeout):
            if attempt == 2:
                raise
            time.sleep(1 * (attempt + 1))

    if response is None:
        raise RuntimeError("GROBID service unavailable after retries")

    root = ET.fromstring(response.text)
    references: list[ReferenceRecord] = []
    for node in root.findall(".//tei:biblStruct", _NAMESPACE):
        references.append(_reference_from_bibl_struct(node))
    return references


def _reference_from_bibl_struct(node: XmlNode) -> ReferenceRecord:
    """Project one GROBID ``biblStruct`` onto our reference contract."""
    analytic_title = _text(node.find("tei:analytic/tei:title", _NAMESPACE))
    monograph_title = _text(node.find("tei:monogr/tei:title", _NAMESPACE))
    identifiers = _identifiers(node)
    doi = normalize_doi(identifiers.get("doi"))
    scopes = _bibl_scopes(node)
    url = _url(node, identifiers)
    title = analytic_title or monograph_title
    grobid: GrobidReference = {
        "title": title,
        "authors": _authors(node),
        "year": _year(node),
        "venue": monograph_title if analytic_title else None,
        "volume": scopes.get("volume"),
        "issue": scopes.get("issue"),
        "pages": scopes.get("pages"),
        "publisher": _text(node.find("tei:monogr/tei:imprint/tei:publisher", _NAMESPACE)),
        "url": url,
        "doi": doi,
        "identifiers": identifiers,
        "raw_reference": _text(node) or "",
        "tei": ET.tostring(node, encoding="unicode"),
    }
    return {
        "raw_reference": grobid["raw_reference"],
        "doi": doi,
        "title": title,
        "authors": grobid["authors"],
        "year": grobid["year"],
        "venue": grobid["venue"],
        "volume": grobid["volume"],
        "issue": grobid["issue"],
        "pages": grobid["pages"],
        "publisher": grobid["publisher"],
        "url": url,
        "grobid": grobid,
    }


def _text(node: XmlNode | None) -> str | None:
    """Return node text with whitespace collapsed, or ``None`` when empty."""
    if node is None:
        return None
    value = " ".join(" ".join(node.itertext()).split())
    return value or None


def _authors(node: XmlNode) -> list[str]:
    """Collect author names in document order without duplicates."""
    names: list[str] = []
    for author in node.findall(".//tei:author", _NAMESPACE):
        person = author.find(".//tei:persName", _NAMESPACE)
        if person is not None:
            forename = _text(person.find("tei:forename", _NAMESPACE))
            surname = _text(person.find("tei:surname", _NAMESPACE))
            name = " ".join(part for part in (forename, surname) if part)
        else:
            name = _text(author.find(".//tei:orgName", _NAMESPACE)) or _text(author)
        if name and name not in names:
            names.append(name)
    return names


def _year(node: XmlNode) -> int | None:
    """Return the publication year GROBID attached to the reference."""
    dates = node.findall(".//tei:date", _NAMESPACE)
    ordered = [date for date in dates if date.attrib.get("type") == "published"]
    ordered.extend(date for date in dates if date not in ordered)
    for date in ordered:
        value = date.attrib.get("when") or _text(date)
        match = _YEAR.search(value or "")
        if match and 1000 <= int(match.group(1)) <= 2999:
            return int(match.group(1))
    return None


def _bibl_scopes(node: XmlNode) -> dict[str, str]:
    """Map GROBID ``biblScope`` units onto volume, issue, and pages."""
    scopes: dict[str, str] = {}
    for scope in node.findall(".//tei:imprint/tei:biblScope", _NAMESPACE):
        key = _SCOPE_NAMES.get(scope.attrib.get("unit", "").lower())
        if not key:
            continue
        value = _text(scope)
        if key == "pages" and scope.attrib.get("from"):
            end = scope.attrib.get("to")
            value = f"{scope.attrib['from']}-{end}" if end else scope.attrib["from"]
        if value:
            scopes[key] = value
    return scopes


def _identifiers(node: XmlNode) -> dict[str, str]:
    """Return every typed identifier GROBID reported for the reference."""
    identifiers: dict[str, str] = {}
    for idno in node.findall(".//tei:idno", _NAMESPACE):
        kind = idno.attrib.get("type", "").strip().lower()
        value = _text(idno)
        if kind and value:
            identifiers.setdefault(kind, value)
    return identifiers


def _url(node: XmlNode, identifiers: dict[str, str]) -> str | None:
    """Prefer a GROBID pointer target, then a typed URL identifier."""
    pointer = node.find(".//tei:ptr", _NAMESPACE)
    target = pointer.attrib.get("target") if pointer is not None else None
    return target or identifiers.get("url")
