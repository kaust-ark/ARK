"""A source that did not answer is not evidence that a paper does not exist.

On 2026-11-05, from mcnode15: DBLP served a bot-challenge page to every API
call, arXiv timed out, and Semantic Scholar and OpenAlex returned 429. Every
lookup read as "no hits". 3b49d53e's first pass verified 0 of 13 entries. The
second pass happened to replace two, which satisfied the drop guard, and LoRA,
T5, EWC and eight more were deleted along with 26 \\cite uses.
"""

import io
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import ark.citation as cit
from ark.latex.compiler import CompilerMixin


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _clean():
    cit.reset_source_health()
    yield
    cit.reset_source_health()


def test_a_bot_challenge_page_marks_the_source_unavailable(monkeypatch):
    page = b"<!doctype html><title>Making sure you're not a bot!</title>"
    monkeypatch.setattr(cit.urllib.request, "urlopen", lambda *a, **k: _Resp(page))
    assert cit._search_dblp("LoRA: Low-Rank Adaptation of Large Language Models") == []
    assert cit.unavailable_sources() == ["dblp.org"]


def test_an_http_error_marks_the_source_unavailable(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(cit.urllib.request, "urlopen", boom)
    assert cit._http_get_json("https://api.semanticscholar.org/graph/v1/paper/search?q=x") is None
    assert cit.unavailable_sources() == ["api.semanticscholar.org"]


def test_a_real_answer_with_no_hits_is_not_an_outage(monkeypatch):
    body = b'{"result": {"hits": {"hit": []}}}'
    monkeypatch.setattr(cit.urllib.request, "urlopen", lambda *a, **k: _Resp(body))
    assert cit._search_dblp("A paper nobody wrote") == []
    assert cit.unavailable_sources() == []


def _verifier(tmp_path, monkeypatch, down):
    """Run the citation loop with one unconfirmed entry and stubbed APIs."""
    (tmp_path / "paper").mkdir()
    (tmp_path / "paper" / "references.bib").write_text("@article{lora, title={LoRA}}\n")
    s = MagicMock()  # every other collaborator is inert
    s.latex_dir = tmp_path / "paper"
    s.state_dir = tmp_path
    s.logs = []
    s.log = lambda m, l="INFO": s.logs.append((l, m))
    s._prune_undefined_citations = MagicMock(return_value=3)

    verified = SimpleNamespace(status="VERIFIED", entry_key="t5")
    unconfirmed = SimpleNamespace(status="NEEDS-CHECK", entry_key="lora")
    removed = []

    def verify(_):
        cit._UNAVAILABLE.update(down)
        return [verified, unconfirmed]

    monkeypatch.setattr(cit, "verify_bib", verify)
    monkeypatch.setattr(cit, "fix_bib", lambda *a: None)
    monkeypatch.setattr(cit, "cleanup_unused", lambda *a: [])
    monkeypatch.setattr(cit, "replace_unverified_entries", lambda *a: ([], ["lora"]))
    monkeypatch.setattr(cit, "remove_entries", lambda p, keys: removed.extend(keys) or keys)
    CompilerMixin._run_citation_verification(s)
    return removed, s.logs


def test_nothing_is_dropped_while_a_source_is_silent(tmp_path, monkeypatch):
    removed, logs = _verifier(tmp_path, monkeypatch, down={"dblp.org"})
    assert removed == []
    assert any("dblp.org did not answer" in m for _, m in logs)


def test_unconfirmable_entries_are_still_dropped_when_every_source_answered(tmp_path, monkeypatch):
    removed, _ = _verifier(tmp_path, monkeypatch, down=set())
    assert removed == ["lora"]
