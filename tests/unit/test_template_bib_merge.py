"""Unit tests for the venue-template .bib merge into references.bib."""
import textwrap
from pathlib import Path

from ark.pipeline import PipelineMixin


# Mock orchestrator with just the methods the bib merge helper needs.
# `_split_top_level_bib_entries` is a @staticmethod in PipelineMixin — when we
# copy it to another class via simple assignment Python rebinds it as a regular
# method (it loses its staticmethod-ness across the class boundary). Wrap with
# staticmethod() to preserve the no-self signature.
class _MockOrch:
    _merge_template_bibs_into_references = (
        PipelineMixin._merge_template_bibs_into_references
    )
    _split_top_level_bib_entries = staticmethod(
        PipelineMixin._split_top_level_bib_entries
    )

    def __init__(self, latex_dir: Path):
        self.latex_dir = latex_dir
        self.log_calls: list[tuple[str, str]] = []

    def log(self, msg: str, level: str = "INFO"):
        self.log_calls.append((level, msg))


def _seed(latex_dir: Path, files: dict[str, str]):
    latex_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (latex_dir / name).write_text(textwrap.dedent(content).lstrip())


# Core: merge custom.bib into references.bib

def test_merges_custom_bib_into_empty_references(tmp_path):
    _seed(tmp_path, {
        "references.bib": "% ARK auto-managed references\n\n",
        "custom.bib": """
            @inproceedings{vaswani2017attention,
              title={Attention Is All You Need},
              author={Vaswani, Ashish and others},
              year={2017}
            }
            @misc{devlin2019bert,
              title={BERT},
              author={Devlin, Jacob},
              year={2019}
            }
            """,
    })
    orch = _MockOrch(tmp_path)
    orch._merge_template_bibs_into_references()

    refs = (tmp_path / "references.bib").read_text()
    assert "vaswani2017attention" in refs
    assert "devlin2019bert" in refs
    assert (tmp_path / "custom.bib").exists()
    assert any("Merged 2 bib entries" in m for _, m in orch.log_calls)


def test_skips_keys_already_in_references(tmp_path):
    _seed(tmp_path, {
        "references.bib": """
            @inproceedings{vaswani2017attention,
              title={Already Here},
              author={Whoever},
              year={2017}
            }
            """,
        "custom.bib": """
            @inproceedings{vaswani2017attention,
              title={Different Copy},
              year={2017}
            }
            @misc{new_one,
              title={Brand New},
              year={2024}
            }
            """,
    })
    orch = _MockOrch(tmp_path)
    orch._merge_template_bibs_into_references()
    refs = (tmp_path / "references.bib").read_text()
    assert refs.count("vaswani2017attention") == 1
    assert "Already Here" in refs
    assert "Different Copy" not in refs
    assert "new_one" in refs


def test_idempotent_no_duplicates_on_second_run(tmp_path):
    _seed(tmp_path, {
        "references.bib": "% ARK auto-managed references\n\n",
        "custom.bib": """
            @misc{key1, title={Foo}, year=2020}
            @misc{key2, title={Bar}, year=2021}
            """,
    })
    orch = _MockOrch(tmp_path)
    orch._merge_template_bibs_into_references()
    first = (tmp_path / "references.bib").read_text()
    orch._merge_template_bibs_into_references()
    second = (tmp_path / "references.bib").read_text()
    assert first == second
    assert second.count("key1") == 1
    assert second.count("key2") == 1


def test_handles_nested_braces_in_titles(tmp_path):
    """Titles like ``{The {ACL} Special Theme}`` contain nested braces.
    The split routine must capture the entry without truncating at the
    inner closing brace."""
    _seed(tmp_path, {
        "references.bib": "% empty\n",
        "custom.bib": """
            @inproceedings{nested2026,
              title={The {ACL} 2026 Special Theme},
              author={Author, A.},
              year={2026}
            }
            @misc{plain2026,
              title={Plain Title},
              year={2026}
            }
            """,
    })
    orch = _MockOrch(tmp_path)
    orch._merge_template_bibs_into_references()
    refs = (tmp_path / "references.bib").read_text()
    assert "nested2026" in refs
    assert "plain2026" in refs
    assert "{ACL}" in refs


def test_no_op_when_no_template_bibs_present(tmp_path):
    _seed(tmp_path, {
        "references.bib": "% only this file\n@misc{a, title={A}}\n",
    })
    orch = _MockOrch(tmp_path)
    orch._merge_template_bibs_into_references()
    refs = (tmp_path / "references.bib").read_text()
    assert "@misc{a" in refs
    assert not any("Merged" in m for _, m in orch.log_calls)


def test_creates_references_if_missing(tmp_path):
    _seed(tmp_path, {
        "custom.bib": """
            @misc{foo, title={Foo}}
            """,
    })
    orch = _MockOrch(tmp_path)
    orch._merge_template_bibs_into_references()
    refs_path = tmp_path / "references.bib"
    assert refs_path.exists()
    assert "foo" in refs_path.read_text()


# Real-world regression: paper 2's exact scenario

def test_paper2_scenario_acl_template_bug(tmp_path):
    """Reproduces paper 2's actual broken state:

    - references.bib has ARK's auto-managed marker + only [NEEDS-CHECK] notes
    - custom.bib has 16 ACL starter entries
    - bibliography{custom} in main.tex points at custom.bib

    After merge, references.bib should include all 16 keys, so the writer
    prompt's "use keys from references.bib" rule can succeed.
    """
    _seed(tmp_path, {
        "references.bib": """
            % ARK auto-managed references

            % [NEEDS-CHECK] Not found in DBLP, CrossRef, or Semantic Scholar
            % [NEEDS-CHECK: citation not verified]
            """,
        "custom.bib": """
            @misc{mlebench2024, title={MLE-Bench}, year=2024}
            @inproceedings{mlragentbench2024, title={MLAgentBench}, year=2024}
            @misc{rebench2024, title={RE-Bench}, year=2024}
            @inproceedings{scienceagentbench2024, title={ScienceAgentBench}, year=2025}
            @article{corebench2024, title={CORE-Bench}, year=2025}
            @inproceedings{discoverybench2024, title={DiscoveryBench}, year=2024}
            @inproceedings{mlrbench2025, title={MLR-Bench}, year=2025}
            @inproceedings{hidden_pitfalls_2025, title={Hidden Pitfalls}, year=2025}
            @misc{abram_2026, title={Abram 2026}, year=2026}
            @misc{aisciv2_2025, title={AI Scientist v2}, year=2025}
            @misc{agentlab2025, title={Agent Laboratory}, year=2025}
            @inproceedings{claimcheck2025, title={CLAIMCHECK}, year=2025}
            @misc{citeguard2025, title={CiteGuard}, year=2025}
            @misc{facts2025, title={FACTS}, year=2025}
            @inproceedings{citdrift2025, title={Citation Drift}, year=2025}
            @misc{anon_supplementary, title={Anon Supplementary}, year=2026}
            """,
    })
    orch = _MockOrch(tmp_path)
    orch._merge_template_bibs_into_references()
    refs = (tmp_path / "references.bib").read_text()
    expected_keys = [
        "mlebench2024", "mlragentbench2024", "rebench2024",
        "scienceagentbench2024", "corebench2024", "discoverybench2024",
        "mlrbench2025", "hidden_pitfalls_2025", "abram_2026",
        "aisciv2_2025", "agentlab2025", "claimcheck2025", "citeguard2025",
        "facts2025", "citdrift2025", "anon_supplementary",
    ]
    for k in expected_keys:
        assert k in refs, f"missing key after merge: {k}"
