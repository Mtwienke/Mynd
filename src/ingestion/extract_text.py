from pathlib import Path
import hashlib
import pymupdf


def extract_pdf(path: Path) -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    with pymupdf.open(path) as pdf:
        pages = [
            {
                "page": i + 1,
                "text": page.get_text("text"),
            }
            for i, page in enumerate(pdf)
        ]

    return {
        "sha256": digest,
        "page_count": len(pages),
        "pages": pages,
    }