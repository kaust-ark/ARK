"""Chat follows the selected runtime, and its usage reaches the ledger.

Chat always ran `openhands --resume`, so a DSH host could not chat at all, and
the chat ledger only counted turns with a positive USD cost, which DSH never
reports.
"""
import json
from types import SimpleNamespace

import ark.chat_agent as chat
from ark.pipeline import PipelineMixin


class FakeDSH:
    made = []

    def __init__(self, model, *, resume_session_id=None, next_turn=False):
        self.resume_session_id, self.next_turn = resume_session_id, next_turn
        self.session_id = resume_session_id or "b" * 32
        FakeDSH.made.append(self)

    def execute(self, prompt, path_boundary, code_dir, timeout, **kw):
        self.prompt = prompt
        return 0, "", "", 1, False

    def parse_output(self, stdout):
        return {"result": " Answer. ", "error_code": None,
                "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 50,
                          "cache_creation_tokens": 0, "cost_usd": None}}


def _turn(monkeypatch, tmp_path, conv=None):
    import ark.engines.dsh_runtime as dr
    monkeypatch.setenv("ARK_AGENT_RUNTIME", "dsh")
    monkeypatch.setattr(dr, "DSHRuntime", FakeDSH)
    return chat.run_chat_turn(workspace=tmp_path, message="How is it going?", model="openai/m",
                              conversation_id=conv, log=lambda *a: None)


def test_a_dsh_chat_continues_one_session_and_sums_its_usage(monkeypatch, tmp_path):
    FakeDSH.made.clear()
    answer, conv, ok = _turn(monkeypatch, tmp_path)
    assert (answer, conv, ok) == ("Answer.", "dsh:" + "b" * 32, True)
    assert chat.load_conversation_id(tmp_path) == conv
    _turn(monkeypatch, tmp_path, conv)
    second = FakeDSH.made[-1]
    assert second.resume_session_id == "b" * 32 and second.next_turn
    assert second.prompt == "How is it going?"
    usage = chat.read_conversation_usage(tmp_path, conv)
    assert usage["input_tokens"] == 200 and usage["output_tokens"] == 20
    assert usage["cost_usd"] is None


def test_an_openhands_conversation_id_starts_a_new_dsh_session(monkeypatch, tmp_path):
    FakeDSH.made.clear()
    _turn(monkeypatch, tmp_path, "8f1c2d3e-openhands-id")
    assert FakeDSH.made[-1].resume_session_id is None


def test_runtime_switch_preserves_both_chat_conversations(monkeypatch, tmp_path):
    chat.save_conversation_id(tmp_path, "original-openhands")
    _, dsh_id, _ = _turn(monkeypatch, tmp_path, "original-openhands")
    monkeypatch.setenv("ARK_AGENT_RUNTIME", "cli")
    assert chat.load_conversation_id(tmp_path) == "original-openhands"
    monkeypatch.delenv("ARK_AGENT_RUNTIME")
    assert chat.load_conversation_id(tmp_path) == dsh_id


def test_chat_cost_deltas_are_scoped_to_the_conversation(tmp_path):
    p = PipelineMixin.__new__(PipelineMixin)
    p.code_dir, p._agent_stats = tmp_path, []
    p.log = lambda *a, **k: None
    p._write_cost_report = lambda: None
    chat.save_conversation_id(tmp_path, "old")
    (tmp_path / ".ark_chat_cost.json").write_text(json.dumps({
        "input_tokens": 100, "cost_usd": 1.0,
    }))
    dsh_id = "dsh:" + "b" * 32
    chat._add_turn_usage(tmp_path, dsh_id, {"input_tokens": 5, "cost_usd": 0.25})
    p._record_chat_cost(chat.read_conversation_usage(tmp_path, dsh_id), dsh_id)
    p._record_chat_cost({"input_tokens": 101, "cost_usd": 1.5}, "old")
    assert [x["input_tokens"] for x in p._agent_stats] == [5, 1]
    assert [x["cost_usd"] for x in p._agent_stats] == [0.25, 0.5]

def test_unpriced_chat_tokens_reach_the_ledger(tmp_path):
    p = PipelineMixin.__new__(PipelineMixin)
    p.code_dir, p._agent_stats, p.logs = tmp_path, [], []
    p.log = lambda m, l="INFO": p.logs.append(m)
    p._write_cost_report = lambda: None
    usage = {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 50,
             "cache_creation_tokens": 0, "cost_usd": None}
    p._record_chat_cost(usage)
    assert p._agent_stats[-1]["input_tokens"] == 100
    assert p._agent_stats[-1]["cost_usd"] is None
    assert "cost unknown" in p.logs[-1]
    p._record_chat_cost(usage)  # same cumulative total: nothing new
    assert len(p._agent_stats) == 1


def test_priced_chat_turns_are_unchanged(tmp_path):
    p = PipelineMixin.__new__(PipelineMixin)
    p.code_dir, p._agent_stats = tmp_path, []
    p.log = lambda *a, **k: None
    p._write_cost_report = lambda: None
    p._record_chat_cost({"input_tokens": 5, "output_tokens": 1, "cache_read_tokens": 0,
                         "cache_creation_tokens": 0, "cost_usd": 0.25})
    assert p._agent_stats[-1]["cost_usd"] == 0.25
