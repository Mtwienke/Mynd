from pydantic import BaseModel


class ReferenceCandidate(BaseModel):
    raw_text: str
    title: str | None = None
    authors: list[str] = []
    year: int | None = None
    doi: str | None = None


class CitationMatch(BaseModel):
    reference: ReferenceCandidate
    work_id: str | None = None
    confidence: float = 0.0
    status: str = "unresolved"
    evidence: str | None = None