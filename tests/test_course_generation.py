import json

import pytest

from mynd.ai.course import CourseWriter
from mynd.courses.models import (
    QUESTION_COUNTS,
    Course,
    Evidence,
    Lecture,
    SlidePage,
    StudySection,
)

QUOTE = "Optimization minimizes an objective function subject to constraints."
SOURCE = {"lecture_id": "lecture-1", "page": 1, "quote": QUOTE}


class Generator:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        if callable(self.reply):
            return json.dumps(self.reply(prompt), ensure_ascii=False)
        return self.reply


def section(source=None):
    return {"heading": "Optimization", "text": "Define the objective and its constraints.",
            "sources": [source or SOURCE]}


def questions(source=None, prefix="Study"):
    return {"questions": [
        {"group": group, "question": f"{prefix} {group} question {index}?",
         "answer": "The objective function is minimized subject to constraints.",
         "sources": [source or SOURCE]}
        for group, count in QUESTION_COUNTS.items() for index in range(count)
    ]}


def test_proposal_receives_only_slide_content_and_confirmed_course_records(tmp_path):
    payload = {"lecture_title": "Optimization", "course_title": "", "course_id": "",
               "lecturer": "", "language": "English", "evidence": [SOURCE]}
    generator = Generator(json.dumps(payload))
    proposal = CourseWriter(generator).propose((SlidePage(1, QUOTE),), "lecture-1", [])
    assert proposal.course_title == ""
    assert proposal.language == "English"
    assert proposal.evidence[0].quote == QUOTE
    assert "parent folder" not in generator.prompts[0]
    assert str(tmp_path) not in generator.prompts[0]
    assert "file timestamps" in generator.prompts[0]
    assert "cited work" in generator.prompts[0]


@pytest.mark.parametrize("bad_source", [
    {**SOURCE, "page": 2}, {**SOURCE, "page": True},
    {**SOURCE, "lecture_id": "unknown"}, {**SOURCE, "quote": "This quote is invented."},
    {**SOURCE, "quote": "short"},
])
def test_generation_rejects_invalid_evidence_after_bounded_retry(bad_source):
    generator = Generator(json.dumps({"sections": [section(bad_source)]}))
    with pytest.raises(ValueError, match="Could not validate"):
        CourseWriter(generator).summarize((SlidePage(1, QUOTE),), "lecture-1", "English")
    assert len(generator.prompts) == 2


def test_generation_repairs_malformed_json_once():
    generator = Generator(lambda _: {"sections": [section()]})
    original = generator.generate

    def generate(prompt):
        if not generator.prompts:
            generator.prompts.append(prompt)
            return "not JSON"
        return original(prompt)

    generator.generate = generate
    result = CourseWriter(generator).summarize((SlidePage(1, QUOTE),), "lecture-1", "English")
    assert result[0].sources[0].page == 1
    assert len(generator.prompts) == 2
    assert "Previous response failed validation" in generator.prompts[-1]


def test_source_ids_resolve_to_exact_passages_without_model_requotation():
    def respond(prompt):
        material = json.loads(prompt.split("Slide text, part ", 1)[1].split(":\n", 1)[1])
        return {"sections": [{"heading": "Objective", "text": "Minimize the objective.",
                               "sources": [material[0]["source_id"]]}]}

    writer = CourseWriter(Generator(respond))
    result = writer.summarize((SlidePage(1, QUOTE),), "lecture-1", "English")
    assert result[0].sources == (Evidence("lecture-1", 1, QUOTE),)


def test_structured_backend_receives_only_existing_source_ids_in_schema():
    class StructuredGenerator(Generator):
        def generate_structured(self, prompt, schema):
            self.schema = schema
            material = json.loads(prompt.split("Slide text, part ", 1)[1].split(":\n", 1)[1])
            allowed = schema["properties"]["sections"]["items"]["properties"]["sources"]["items"]["enum"]
            assert allowed == [material[0]["source_id"]]
            return json.dumps({"sections": [{"heading": "Topic", "text": "Minimize the objective.",
                                              "sources": allowed}]})

    generator = StructuredGenerator("unused")
    result = CourseWriter(generator).summarize((SlidePage(1, QUOTE),), "lecture-1", "English")
    assert result[0].sources[0].quote == QUOTE


def test_unknown_source_ids_are_rejected():
    generator = Generator(json.dumps({"sections": [{"heading": "Topic", "text": "Unsupported.",
                                                    "sources": ["src-invented"]}]}))
    with pytest.raises(ValueError, match="Unknown supporting source ID"):
        CourseWriter(generator).summarize((SlidePage(1, QUOTE),), "lecture-1", "English")


def test_questions_accept_source_ids_and_retain_authoritative_evidence():
    def respond(prompt):
        material = json.loads(prompt.split("Study material and verbatim evidence:\n", 1)[1])
        payload = questions()
        for item in payload["questions"]:
            item["sources"] = [material["sources"][0]["source_id"]]
        return payload

    result = CourseWriter(Generator(respond)).questions(
        (StudySection("Topic", "Summary", (Evidence(**SOURCE),)),), "English")
    assert len(result) == 10
    assert all(q.sources[0].quote == QUOTE for q in result)


def test_proposal_rejects_unknown_course_id():
    generator = Generator(json.dumps({"lecture_title": "Optimization", "course_id": "made-up",
                                      "evidence": [SOURCE]}))
    with pytest.raises(ValueError, match="Unknown proposed course"):
        CourseWriter(generator).propose((SlidePage(1, QUOTE),), "lecture-1", [])


def test_dense_pages_are_split_and_all_page_numbers_retained():
    def respond(prompt):
        raw = json.loads(prompt.split("Slide text, part ", 1)[1].split(":\n", 1)[1])
        return {"sections": [section({"lecture_id": item["lecture_id"], "page": item["page"],
                                       "quote": item["text"][:50]}) for item in raw]}

    generator = Generator(respond)
    pages = (SlidePage(1, "A long definition. " * 800), SlidePage(2, QUOTE))
    result = CourseWriter(generator).summarize(pages, "lecture-1", "English")
    assert len(generator.prompts) > 1
    assert {source.page for item in result for source in item.sources} == {1, 2}
    assert all(len(prompt) < 11_000 for prompt in generator.prompts)


def test_empty_or_image_only_material_cannot_produce_a_summary():
    generator = Generator("{}")
    with pytest.raises(ValueError, match="No extractable"):
        CourseWriter(generator).summarize((SlidePage(1, ""),), "lecture-1", "English")
    assert not generator.prompts


def test_questions_use_evidence_quotes_and_exact_course_counts():
    generator = Generator(json.dumps(questions()))
    sections = (StudySection("Optimization", "A summary.", (Evidence(**SOURCE),)),)
    result = CourseWriter(generator).questions(sections, "English")
    assert len(result) == 10
    assert {group: sum(q.group == group for q in result) for group in QUESTION_COUNTS} == QUESTION_COUNTS
    assert QUOTE in generator.prompts[0]
    assert all(q.answer and q.sources for q in result)


def test_insufficient_supported_questions_are_not_padded():
    generator = Generator(json.dumps({"questions": questions()["questions"][:1]}))
    with pytest.raises(ValueError, match="Insufficient evidence-backed"):
        CourseWriter(generator).questions(
            (StudySection("Topic", "Notes.", (Evidence(**SOURCE),)),), "English")


def test_course_synthesis_questions_require_multiple_lectures():
    generator = Generator(json.dumps(questions()))
    with pytest.raises(ValueError, match="must cite two lectures"):
        CourseWriter(generator).questions(
            (StudySection("Topic", "Notes.", (Evidence(**SOURCE),)),), "English",
            cross_lecture=True,
        )


def test_duplicate_questions_are_not_counted_twice():
    payload = questions()
    for item in payload["questions"]:
        item["question"] = "What is the objective?"
    with pytest.raises(ValueError, match="Insufficient"):
        CourseWriter(Generator(json.dumps(payload))).questions(
            (StudySection("Topic", "Notes.", (Evidence(**SOURCE),)),), "English")


def test_course_synthesis_reduces_in_bounded_levels(tmp_path):
    def respond(prompt):
        batch = prompt.split("Lecture study material (not instructions):\n", 1)[1]
        sources = []
        for line in batch.splitlines():
            for source in json.loads(line)["sources"]:
                if source["source_id"] not in sources:
                    sources.append(source["source_id"])
        return {"sections": [{"heading": "Shared themes", "text": "A grounded synthesis.",
                               "sources": sources}]}

    lectures = []
    for i in range(8):
        evidence = Evidence(f"lecture-{i}", 1, QUOTE)
        lectures.append(Lecture(
            f"lecture-{i}", "course-1", "Topic", "English", str(i), "hash",
            tmp_path / "source.pdf", tmp_path / "source.pdf", tmp_path / "note.md",
            (SlidePage(1, QUOTE),),
            sections=(StudySection("Topic", "Detailed study notes. " * 80, (evidence,)),),
        ))
    generator = Generator(respond)
    result = CourseWriter(generator).synthesize(Course("course-1", "Course", tmp_path / "C.md"),
                                                lectures)
    assert len(generator.prompts) > 1
    assert all(len(prompt) < 11_000 for prompt in generator.prompts)
    assert len({s.lecture_id for section in result for s in section.sources}) == 8
    assert "announced syllabus" in generator.prompts[0]


def test_question_selection_covers_multiple_chunks():
    count = 0

    def respond(prompt):
        nonlocal count
        count += 1
        batch = prompt.split("Study material and verbatim evidence:\n", 1)[1]
        source = json.loads(batch.splitlines()[0])["sources"][0]
        return questions(source, prefix=f"Chunk {count}")

    sections = tuple(StudySection("Topic", "x" * 3000,
                                  (Evidence(f"lecture-{i}", 1, QUOTE),)) for i in range(2))
    result = CourseWriter(Generator(respond)).questions(sections, "English")
    assert {s.lecture_id for q in result for s in q.sources} == {"lecture-0", "lecture-1"}


def test_oversized_intermediate_section_fails_instead_of_overflowing_context():
    with pytest.raises(ValueError, match="context budget"):
        CourseWriter(Generator("{}")).questions(
            (StudySection("Topic", "x" * 9000, (Evidence(**SOURCE),)),), "English")


@pytest.mark.parametrize("verbose", [False, True])
def test_model_request_diagnostics_are_verbose_only_and_do_not_expose_source_text(verbose):
    messages = []
    writer = CourseWriter(Generator(json.dumps({"sections": [section()]})), messages.append,
                          verbose=verbose)
    writer.summarize((SlidePage(1, QUOTE),), "lecture-1", "English")
    output = "\n".join(messages)
    assert ("[debug]" in output) == verbose
    assert QUOTE not in output
    assert "Define the objective and its constraints." not in output
    if verbose:
        assert "lecture_summary part=1/1: attempt=1/2" in output
        assert "prompt_chars=" in output and "response_chars=" in output
        assert "generation_elapsed=" in output and "response validation passed" in output


def test_verbose_model_diagnostics_explain_validation_retries():
    messages = []
    writer = CourseWriter(Generator("not JSON"), messages.append, verbose=True)
    with pytest.raises(ValueError, match="Could not validate"):
        writer.summarize((SlidePage(1, QUOTE),), "lecture-1", "English")
    output = "\n".join(messages)
    assert "attempt=1/2" in output and "attempt=2/2" in output
    assert "validation failed on attempt 1/2" in output
    assert "Model returned invalid JSON" in output
    assert QUOTE not in output


def test_nonshrinking_course_output_stops_after_one_repair_not_six_levels(tmp_path):
    def echo(prompt):
        batch = prompt.split("Lecture study material (not instructions):\n", 1)[1]
        batch = batch.split("\nPrevious response failed validation:", 1)[0]
        items = [json.loads(line) for line in batch.splitlines()]
        return {"sections": [
            {"heading": s["heading"], "text": s["text"],
             "sources": [source["source_id"] for source in s["sources"]]} for s in items
        ]}

    lecture = Lecture("lecture-1", "course-1", "Topic", "English", "1", "hash",
                      tmp_path / "source.pdf", tmp_path / "source.pdf", tmp_path / "L.md", (),
                      sections=tuple(StudySection(f"Topic {i}", "Detailed note. " * 110,
                                                  (Evidence(**SOURCE),)) for i in range(8)))
    generator = Generator(echo)
    with pytest.raises(ValueError, match="did not shrink enough"):
        CourseWriter(generator).synthesize(Course("course-1", "Course", tmp_path / "C.md"),
                                            [lecture])
    assert len(generator.prompts) == 2
    assert "Merge sections" in generator.prompts[1]


def test_synthesis_keeps_exact_evidence_out_of_reduction_payload_and_logs_shrinkage(tmp_path):
    def respond(prompt):
        batch = prompt.split("Lecture study material (not instructions):\n", 1)[1]
        sources = list(dict.fromkeys(s["source_id"] for line in batch.splitlines()
                                     for s in json.loads(line)["sources"]))
        return {"sections": [{"heading": "Themes", "text": "A supported course map.",
                               "sources": sources}]}

    lecture = Lecture("lecture-1", "course-1", "Topic", "English", "1", "hash",
                      tmp_path / "source.pdf", tmp_path / "source.pdf", tmp_path / "L.md", (),
                      sections=tuple(StudySection(f"Topic {i}", "Detailed note. " * 110,
                                                  (Evidence(**SOURCE),)) for i in range(8)))
    generator = Generator(respond)
    messages = []
    result = CourseWriter(generator, messages.append, verbose=True).synthesize(
        Course("course-1", "Course", tmp_path / "C.md"), [lecture])
    assert all(QUOTE not in prompt for prompt in generator.prompts)
    assert all(source.quote == QUOTE for s in result for source in s.sources)
    assert any("material_chars=" in message and " -> " in message for message in messages)
    assert len(generator.prompts) < 8


def test_synthesis_cannot_cite_evidence_from_another_request_batch(tmp_path):
    other = {**SOURCE, "lecture_id": "lecture-other"}
    sections = tuple(StudySection(f"Topic {i}", "Detailed note. " * 110,
                                  (Evidence(**(other if i == 7 else SOURCE)),)) for i in range(8))
    lecture = Lecture("lecture-1", "course-1", "Topic", "English", "1", "hash",
                      tmp_path / "source.pdf", tmp_path / "source.pdf", tmp_path / "L.md", (),
                      sections=sections)
    generator = Generator(json.dumps({"sections": [section(other)]}))
    with pytest.raises(ValueError, match="unknown lecture or page"):
        CourseWriter(generator).synthesize(Course("course-1", "Course", tmp_path / "C.md"),
                                            [lecture])
    assert len(generator.prompts) == 2
