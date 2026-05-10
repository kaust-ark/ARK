"""Unit tests for ark.findings_schema.attempt_repair.

Covers the dominant agent-emitted YAML mistake: a sibling top-level key
left indented inside the ``findings:`` list scope.
"""
import yaml

from ark.findings_schema import attempt_repair


# ────────────────────────────────────────────────────────────────────────
#  Common malformation pattern: sibling key indented at 2 spaces inside
#  a `findings:` list
# ────────────────────────────────────────────────────────────────────────


def test_repair_sanity_report_indented_inside_findings_list():
    bad = (
        "findings:\n"
        "  - id: f1\n"
        "    title: First finding\n"
        "  - id: f2\n"
        "    title: Second finding\n"
        "\n"
        "  sanity_report:\n"
        "    acknowledged:\n"
        "      - item one\n"
    )
    repaired, changes = attempt_repair(bad)
    assert repaired is not None, "repair should succeed for the dominant pattern"
    assert any("sanity_report" in c for c in changes)
    parsed = yaml.safe_load(repaired)
    assert "findings" in parsed
    assert "sanity_report" in parsed
    assert isinstance(parsed["findings"], list)
    assert len(parsed["findings"]) == 2
    assert parsed["sanity_report"]["acknowledged"] == ["item one"]


def test_repair_coverage_indented_inside_findings_list():
    bad = (
        "findings:\n"
        "  - id: f1\n"
        "    title: T\n"
        "\n"
        "  coverage:\n"
        "    item_a:\n"
        "      status: done\n"
    )
    repaired, _ = attempt_repair(bad)
    assert repaired is not None
    parsed = yaml.safe_load(repaired)
    assert "coverage" in parsed
    assert parsed["coverage"]["item_a"]["status"] == "done"


# ────────────────────────────────────────────────────────────────────────
#  Repair must NOT touch valid YAML
# ────────────────────────────────────────────────────────────────────────


def test_repair_returns_none_for_valid_yaml():
    good = (
        "findings:\n"
        "  - id: f1\n"
        "    title: T\n"
        "sanity_report:\n"
        "  acknowledged: [a, b]\n"
    )
    repaired, changes = attempt_repair(good)
    assert repaired is None
    assert changes == []


# ────────────────────────────────────────────────────────────────────────
#  Repair must be conservative — don't dedent unknown / nested keys
# ────────────────────────────────────────────────────────────────────────


def test_repair_does_not_dedent_unknown_keys():
    """Some_random_key at 2-space indent inside findings list could be
    intentional (e.g., a deliberately-nested mapping ARK doesn't know
    about). We only dedent identifiers from KNOWN_TOP_LEVEL_KEYS."""
    bad = (
        "findings:\n"
        "  - id: f1\n"
        "    title: T\n"
        "\n"
        "  weird_custom_key:\n"
        "    foo: bar\n"
    )
    repaired, changes = attempt_repair(bad)
    assert repaired is None  # not a confident repair
    assert any("did not match" in c.lower() or "still malformed" in c.lower()
               for c in changes) or changes == ["YAML parse error did not match the known indent-misplacement pattern"]


# ────────────────────────────────────────────────────────────────────────
#  When repair leaves the file still malformed, return None + diagnostics
# ────────────────────────────────────────────────────────────────────────


def test_repair_returns_none_when_other_errors_remain():
    """File has both the known mistake AND an unrelated parse error;
    we should still return None because the repair didn't make the file
    parse cleanly."""
    bad = (
        "findings:\n"
        "  - id: f1\n"
        "  sanity_report:\n"   # known: misplaced sibling
        "    foo: bar\n"
        "open_quotes: 'this never closes\n"  # unrelated: unterminated string
    )
    repaired, changes = attempt_repair(bad)
    assert repaired is None
    # We did make at least the known repair attempt
    assert any("sanity_report" in c for c in changes)
    # And we noted that the result still didn't parse
    assert any("still malformed" in c.lower() for c in changes)


# ────────────────────────────────────────────────────────────────────────
#  Idempotency — running on already-repaired text yields no changes
# ────────────────────────────────────────────────────────────────────────


def test_repair_idempotent_on_clean_yaml():
    bad = (
        "findings:\n"
        "  - id: f1\n"
        "  sanity_report:\n"
        "    foo: bar\n"
    )
    repaired1, _ = attempt_repair(bad)
    assert repaired1 is not None
    # Running attempt_repair on an already-clean file returns None
    repaired2, _ = attempt_repair(repaired1)
    assert repaired2 is None


# ────────────────────────────────────────────────────────────────────────
#  Real-world regression: the actual paper-1 failure
# ────────────────────────────────────────────────────────────────────────


def test_repair_real_world_paper1_pattern():
    """Mimics the structure of paper 1's findings.yaml that triggered
    'expected <block end>, but found ?' at line 465."""
    bad = (
        "coverage:\n"
        "  phase1: {status: done}\n"
        "  phase2: {status: done}\n"
        "\n"
        "findings:\n"
        "  - id: f1\n"
        "    title: First\n"
        "  - id: f2\n"
        "    title: Second\n"
        "  - id: f3\n"
        "    title: Third\n"
        "    supports_claim: \"All claims numerically supported\"\n"
        "\n"
        "  sanity_report:\n"
        "    acknowledged:\n"
        "      - One acknowledged thing\n"
        "      - Another acknowledged thing\n"
    )
    repaired, changes = attempt_repair(bad)
    assert repaired is not None
    parsed = yaml.safe_load(repaired)
    assert set(parsed.keys()) == {"coverage", "findings", "sanity_report"}
    assert len(parsed["findings"]) == 3
    assert len(parsed["sanity_report"]["acknowledged"]) == 2
