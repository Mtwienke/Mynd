"""Small JSON schemas constrain the local decoder to existing source identifiers."""


def _object(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def response_schema(kind: str, source_ids: list[str], course_ids: list[str] | None = None) -> dict:
    source_list = {
        "type": "array",
        "minItems": 1,
        "maxItems": 12,
        "items": {"type": "string", "enum": source_ids},
    }
    if kind == "proposal":
        properties = {
            key: {"type": "string"}
            for key in (
                "lecture_title", "course_title", "university", "semester",
                "lecturer", "language", "order",
            )
        }
        properties["course_id"] = {"type": "string", "enum": ["", *(course_ids or [])]}
        properties["evidence"] = source_list
        return _object(properties)
    if kind == "sections":
        item = _object({
            "heading": {"type": "string", "minLength": 1, "maxLength": 200},
            "text": {"type": "string", "minLength": 1, "maxLength": 1800},
            "sources": source_list,
        })
        return _object({"sections": {
            "type": "array", "minItems": 1, "maxItems": 40, "items": item,
        }})
    if kind == "questions":
        item = _object({
            "group": {"type": "string", "enum": ["concepts", "application", "synthesis"]},
            "question": {"type": "string", "minLength": 1},
            "answer": {"type": "string", "minLength": 1},
            "sources": source_list,
        })
        return _object({"questions": {"type": "array", "maxItems": 10, "items": item}})
    raise ValueError(f"Unknown course response kind: {kind}")
