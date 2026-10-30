from pathlib import Path

import httpx
from defusedxml import ElementTree as ET

from resolution.doi import normalize_doi
from resolution.metadata import ReferenceRecord


def extract_grobid_references(
    pdf_path: Path,
    base_url: str = "http://localhost:8070",
    timeout: float = 90.0,
) -> list[ReferenceRecord]:
    """Extract bibliography records from a PDF using a running GROBID service."""
    with pdf_path.open("rb") as handle:
        response = httpx.post(
            f"{base_url.rstrip('/')}/api/processReferences",
            files={"input": (pdf_path.name, handle, "application/pdf")},
            data={"consolidateCitations": "0"},
            timeout=timeout,
        )
    response.raise_for_status()
    root = ET.fromstring(response.text)
    namespace = {"tei": "http://www.tei-c.org/ns/1.0"}
    references: list[ReferenceRecord] = []
    for node in root.findall(".//tei:biblStruct", namespace):
        raw = " ".join(" ".join(node.itertext()).split())
        doi = next(
            (
                normalized
                for idno in node.findall(".//tei:idno", namespace)
                if idno.attrib.get("type", "").lower() == "doi"
                if (normalized := normalize_doi(idno.text))
            ),
            None,
        )
        title_node = node.find(".//tei:analytic/tei:title", namespace)
        title = " ".join(title_node.itertext()).strip() if title_node is not None else None
        references.append({"raw_reference": raw, "doi": doi, "title": title})
    return references
