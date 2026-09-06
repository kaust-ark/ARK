"""The typed envelope inside a plain Room message, and the HANDOFF line."""

import pytest

from ark.sharednet.typed import (
    HUMAN_REVIEW,
    HUMAN_REVIEW_TAG,
    WORK_RESULT,
    Envelope,
    decode,
    encode,
    handoff_instruction,
    parse_handoff,
)

pytestmark = pytest.mark.unit


def test_encode_decode_round_trip_keeps_text_and_fields():
    content = encode("Draft revised.\n\nNext: reviewer", Envelope(WORK_RESULT, {"next": "reviewer", "done": False}))
    assert content.endswith('sharednet-typed: {"done":false,"next":"reviewer","type":"work.result"}')
    text, envelope = decode(content)
    assert text == "Draft revised.\n\nNext: reviewer"
    assert envelope is not None
    assert envelope.type == WORK_RESULT
    assert envelope.get("next") == "reviewer"
    assert envelope.get("done") is False


def test_untyped_content_decodes_to_no_envelope():
    for content in ("just a human talking", "", "sharednet-typed: not json", "x\nsharednet-typed: []"):
        text, envelope = decode(content)
        assert envelope is None
        assert text == content


def test_human_review_carries_sharednets_tag():
    content = encode("Should we drop the ablation?", Envelope(HUMAN_REVIEW, {"question": "drop ablation?"}))
    assert HUMAN_REVIEW_TAG in content
    _, envelope = decode(content)
    assert envelope.type == HUMAN_REVIEW


@pytest.mark.parametrize(
    "output, expected_next, expected_done",
    [
        ('...work...\nHANDOFF: {"next": "writer", "done": false, "reason": "draft it"}', "writer", False),
        ('HANDOFF: {"next": null, "done": true, "reason": "accepted"}', None, True),
        ("HANDOFF: {'next': 'reviewer', 'done': false}", "reviewer", False),  # single quotes
        ('first\nHANDOFF: {"next": "coder", "done": false}\nlater\nHANDOFF: {"next": "Writer", "done": false}', "writer", False),
        ('HANDOFF: {"next": "none", "done": true}', None, True),
        # Shapes a real Agent produces at the end of a long run.
        ('**HANDOFF:** {"next": "writer", "done": false, "reason": "draft it"}', "writer", False),  # bold
        ('- HANDOFF: {"next": "planner", "done": false}', "planner", False),                        # bullet
        ('> HANDOFF: {"next": "reviewer", "done": false}', "reviewer", False),                      # quoted
        ('HANDOFF:\n```json\n{"next": "coder", "done": false}\n```', "coder", False),              # fenced
        ('HANDOFF: {\n  "next": "writer",\n  "done": false,\n  "reason": "long"\n}', "writer", False),  # pretty-printed
        ('HANDOFF: {"next": "writer", "done": false}.', "writer", False),                           # full stop after
        ('HANDOFF: {"next": "planner", "done": fal', "planner", False),                             # truncated output
        ('HANDOFF: {"next": "writer", "done": false, "reason": "fix {eq:1} in \u00a74"}', "writer", False),  # brace in a value
        ('Handoff: {"next": "reviewer", "done": false}', "reviewer", False),                        # case
    ],
)
def test_parse_handoff_variants(output, expected_next, expected_done):
    handoff = parse_handoff(output)
    assert handoff is not None
    assert handoff.next == expected_next
    assert handoff.done is expected_done


def test_parse_handoff_absent_or_garbage():
    assert parse_handoff("no decision here") is None
    assert parse_handoff("HANDOFF: {garbage}") is None
    assert parse_handoff("") is None


def test_prose_mentioning_the_keyword_is_not_a_decision():
    """The keyword in a sentence must not capture whatever brace comes next."""
    assert parse_handoff("The HANDOFF protocol expects a line like {this}") is None
    assert parse_handoff("I will write the HANDOFF once the run finishes.\n\n"
                         "Config: {\"next\": \"writer\"}") is None


def test_the_last_handoff_wins_across_shapes():
    output = ('draft plan\nHANDOFF: {"next": "coder", "done": false}\n'
              'on reflection\n**HANDOFF:** {"next": "reviewer", "done": false}')
    handoff = parse_handoff(output)
    assert handoff.next == "reviewer"


def test_handoff_instruction_names_the_roles():
    text = handoff_instruction(("writer", "reviewer"))
    assert "writer, reviewer" in text
    assert 'HANDOFF: {"next": "<role>", "done": false' in text


def test_handoff_instruction_says_where_the_line_goes_and_why():
    """Agents dropped the line at the end of long runs; the stakes are stated."""
    text = handoff_instruction(("writer", "reviewer"))
    assert "last line" in text and "code fence" in text
    assert "fixed hand-off order" in text
