from typing import Protocol


class AIProvider(Protocol):
    def summarize_paper(
        self,
        text: str,
        metadata: dict,
    ) -> dict:
        ...

    def generate_questions(
        self,
        source_text: str,
        topic: str,
    ) -> list[dict]:
        ...