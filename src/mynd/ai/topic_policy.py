"""Local, schema-constrained breadth/entity assessment, separate from extraction."""
from __future__ import annotations

import json

POLICY_VERSION = "topic-policy-1-2-3-5-v1"
MINIMUM_SOURCES = {"umbrella": 1, "established": 2, "specialized": 3, "niche": 5}
KINDS = ("concept", "tool", "model_family", "dataset", "benchmark", "metadata")
EXCLUDED_KINDS = {"model_family", "dataset", "benchmark", "metadata"}


def assess_topics(writer, topics: list[dict]) -> dict[str, dict]:
    # Import here to keep TopicWriter's public helper independently callable.
    from mynd.ai.topics import _aliases, _array, _object, _string, _text

    slots = {f"C{i}": topic for i, topic in enumerate(topics)}
    broader_schema = _object({"title": _string(120), "definition": _string(),
                              "aliases": _array(_string(120), 12)})
    item_schema = _object({
        "breadth": {"type": "string", "enum": list(MINIMUM_SOURCES)},
        "kind": {"type": "string", "enum": list(KINDS)},
        "reason": _string(), "broader": _array(broader_schema, 1),
    })
    schema = _object({"assessments": _object({key: item_schema for key in slots})})

    def validate(data):
        assessments = data.get("assessments")
        if not isinstance(assessments, dict) or set(assessments) != set(slots):
            raise ValueError("Supply exactly one assessment for every candidate slot")
        result = {}
        for key, topic in slots.items():
            item = assessments[key]
            if not isinstance(item, dict):
                raise TypeError("Expected a topic assessment")
            if item.get("breadth") not in MINIMUM_SOURCES or item.get("kind") not in KINDS:
                raise ValueError("Unknown topic breadth or entity kind")
            broader = item.get("broader")
            if not isinstance(broader, list) or len(broader) > 1:
                raise ValueError("Suggest at most one nearest broader concept")
            result[topic["id"]] = {
                "breadth": item["breadth"], "kind": item["kind"], "reason": _text(item, "reason"),
                "broader": [{"title": _text(parent, "title", 120),
                             "definition": _text(parent, "definition"), "aliases": _aliases(parent)}
                            for parent in broader],
            }
        return result

    return writer._request(
        "Assess topic breadth",
        "\nClassify each candidate by conceptual BREADTH, not importance or source count. "
        "umbrella (1 source): only broad, tag-like areas such as Artificial Intelligence, "
        "Evolutionary Algorithms, Large Language Models, Machine Learning. "
        "established (2 sources): established subfields/method classes such as Reinforcement "
        "Learning, Convolutional Neural Networks, Physics-Informed Neural Networks, Self-attention. "
        "specialized (3 sources): specific techniques such as LoRA, AdamW, particular optimization "
        "techniques. niche (5 sources): paper-specific architectures, bespoke variants, and narrowly "
        "defined inventions. Never call a particular technique an umbrella just because it is "
        "important or widely used. Explain the classification.\n"
        "kind=concept for general architectures and methods; tool for software/frameworks; "
        "model_family for specific named models/families (GPT, Llama, BERT, etc.); dataset or "
        "benchmark for named research datasets/evaluation suites; metadata for people, organizations, "
        "publication titles, venues, years, generic headings or non-concepts. Model families, "
        "datasets, benchmarks and metadata are NEVER allowed topic nodes, regardless of breadth. "
        "A generic Transformer architecture or CNN method class is a concept, not a product/model family.\n"
        "Optionally suggest ONE nearest genuinely broader conceptual topic (not a synonym), especially "
        "for specialized/niche concepts and excluded resources. For LoRA, Large Language Models may "
        "be suitable. Do not emit chains of ancestors or invent a broader topic for metadata. "
        "This is only a suggestion: the program separately verifies contextual evidence. "
        "A taxonomy relationship alone is NOT supporting evidence. Return no source claims.\n"
        + json.dumps([{**{k: v for k, v in topic.items() if k in {"title", "definition", "aliases"}}, "id": key}
                      for key, topic in slots.items()], ensure_ascii=False), schema, validate,
    )
