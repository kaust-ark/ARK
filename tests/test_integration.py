"""Integration test: full paper iteration with mocked agents.

Mocks all external calls (claude CLI, pdflatex, bibtex, git, mail, squeue)
at the subprocess level and verifies the end-to-end pipeline produces correct
state files, scores, agent call sequence, and cost tracking.
"""

import json
import os
import struct
import sys
import zlib
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
#  Minimal PNG bytes (1x1 white pixel)
# ---------------------------------------------------------------------------

def _minimal_png() -> bytes:
    """Return a valid 1x1 white PNG."""
    def _chunk(ctype, data):
        c = ctype + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = zlib.compress(b"\x00\xff\xff\xff")
    idat = _chunk(b"IDAT", raw)
    iend = _chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


PNG_BYTES = _minimal_png()


# ---------------------------------------------------------------------------
#  MockController: intercepts subprocess.run / subprocess.Popen
# ---------------------------------------------------------------------------

class MockController:
    """Manages all subprocess mock behaviour for the integration test."""

    def __init__(self, state_dir: Path, latex_dir: Path, review_score: float = 7.0):
        self.state_dir = state_dir
        self.latex_dir = latex_dir
        self.review_score = review_score
        self.agent_calls: list[str] = []
        self._reviewer_call_count = 0
        # When True, wrap agent stdout in a claude --output-format json envelope
        # so the JSON parsing path in agents.py is exercised. The cost values
        # below are deterministic so tests can assert exact aggregates.
        self.json_mode = False
        self.json_cost_per_call = 0.025  # USD per agent call when json_mode

    # -- subprocess.run mock ------------------------------------------------

    def subprocess_run(self, cmd, **kwargs):
        if not cmd:
            return self._ok("")

        exe = cmd[0] if isinstance(cmd, (list, tuple)) else cmd

        if exe == "pdflatex":
            # Create fake PDF
            pdf = kwargs.get("cwd", self.latex_dir) / "main.pdf" \
                if isinstance(kwargs.get("cwd"), Path) \
                else Path(kwargs.get("cwd", str(self.latex_dir))) / "main.pdf"
            pdf.write_bytes(b"%PDF-1.4 fake")
            return self._ok("")

        if exe == "bibtex":
            return self._ok("")

        if exe == "git":
            return self._handle_git(cmd, **kwargs)

        if exe == "mail":
            return self._ok("")

        if exe == "squeue":
            return self._ok("")

        if exe == "python":
            return self._ok("")

        if exe == "bash":
            return self._ok("")

        if exe == "claude":
            return self._ok("claude-code 1.0.0")

        # Default: succeed silently
        return self._ok("")

    # -- subprocess.Popen mock ----------------------------------------------

    def subprocess_popen(self, cmd, **kwargs):
        """Mock Popen: detects agent type from the prompt and writes side-effects."""
        prompt = " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        agent_type = self._detect_agent(prompt)
        self.agent_calls.append(agent_type)

        stdout_text = self._agent_stdout(agent_type, prompt)

        # Side effects: reviewer writes latest_review.md, planner writes action_plan.yaml
        if agent_type == "reviewer":
            self._write_review()
        elif agent_type == "planner":
            self._write_action_plan()

        # When json_mode is on, wrap the agent's textual output in a fake claude
        # --output-format json envelope so the JSON-parsing path in agents.py
        # runs end-to-end. Token / cost numbers are deterministic per call so
        # tests can assert on exact aggregates.
        if self.json_mode:
            envelope = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": stdout_text,
                "duration_ms": 1500,
                "duration_api_ms": 1400,
                "total_cost_usd": self.json_cost_per_call,
                "usage": {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 800,
                    "output_tokens": 50,
                },
                "modelUsage": {
                    "claude-opus-4-7[1m]": {
                        "inputTokens": 100,
                        "outputTokens": 50,
                        "cacheReadInputTokens": 800,
                        "cacheCreationInputTokens": 200,
                        "costUSD": self.json_cost_per_call,
                    }
                },
            }
            stdout_text = json.dumps(envelope)

        proc = MagicMock()
        proc.communicate.return_value = (stdout_text, "")
        proc.returncode = 0
        proc.stdout = BytesIO(stdout_text.encode())
        proc.stderr = BytesIO(b"")
        return proc

    # -- helpers ------------------------------------------------------------

    def _ok(self, stdout, returncode=0):
        m = MagicMock()
        m.stdout = stdout
        m.stderr = ""
        m.returncode = returncode
        return m

    def _handle_git(self, cmd, **kwargs):
        subcmd = cmd[1] if len(cmd) > 1 else ""
        if subcmd == "status":
            return self._ok("")  # clean status
        if subcmd == "diff":
            if "--name-only" in cmd:
                return self._ok("")
            if "--numstat" in cmd:
                return self._ok("10\t2\tLatex/main.tex")
            if "--stat" in cmd:
                return self._ok(" Latex/main.tex | 10 +++++++---")
            return self._ok("")
        if subcmd == "commit":
            return self._ok("", returncode=1)  # nothing to commit
        if subcmd == "add":
            return self._ok("")
        return self._ok("")

    def _detect_agent(self, prompt: str) -> str:
        """Detect agent type from the prompt content."""
        agent_types = [
            "visualizer", "reviewer", "planner", "writer",
            "experimenter", "researcher", "meta_debugger", "coder",
        ]
        for at in agent_types:
            # Match [AGENT:xxx] marker from .prompt files
            if f"[AGENT:{at}]" in prompt:
                return at
            if f"{at}.prompt" in prompt:
                return at

        # Fallback: look at prompt content
        prompt_lower = prompt.lower()
        if "review" in prompt_lower:
            return "reviewer"
        if "planner" in prompt_lower or "action_plan" in prompt_lower:
            return "planner"
        if "writer" in prompt_lower or "writing" in prompt_lower:
            return "writer"
        if "figure_config.json" in prompt_lower or "visualizer" in prompt_lower:
            return "visualizer"
        return "unknown"

    def _agent_stdout(self, agent_type: str, prompt: str) -> str:
        # All outputs must be >= 100 chars (stripped) to avoid empty-run detection.
        # The check is: elapsed < 15s AND len(result.strip()) < 100
        filler = ("\nThe agent has completed the requested task successfully. "
                  "All files have been updated according to the instructions provided. "
                  "No errors were encountered during execution.\n")
        if agent_type == "reviewer":
            self._reviewer_call_count += 1
            return (f"Overall Score: {self.review_score}/10\n"
                    f"Review report saved to auto_research/state/latest_review.md\n"
                    f"{filler}")
        if agent_type == "planner":
            return f"Generated action_plan.yaml containing all issues to be addressed\n{filler}"
        if agent_type == "writer":
            return f"Updated main.tex modified Introduction and Results sections\n{filler}"
        if agent_type == "visualizer":
            return f"FIGURES_OK all figure quality checks passed correct dimensions clear fonts\n{filler}"
        if agent_type == "meta_debugger":
            return f"CONTINUE system status normal no issues requiring repair found\n{filler}"
        return f"done task completed successfully\n{filler}"

    def _write_review(self):
        review = (
            f"# Review Report\n\n"
            f"Overall Score: {self.review_score}/10\n\n"
            f"## Major Issues\n"
            f"### M1. Need more experiments\n"
            f"### M2. Improve writing\n\n"
            f"## Minor Issues\n"
            f"### m1. Fix typos\n"
        )
        (self.state_dir / "latest_review.md").write_text(review)

    def _write_action_plan(self):
        plan = {
            "issues": [
                {
                    "id": "M1",
                    "type": "WRITING_ONLY",
                    "title": "Need more experiments description",
                    "description": "Add experiment description",
                    "status": "pending",
                    "actions": [{"agent": "writer", "task": "update experiments section"}],
                },
                {
                    "id": "M2",
                    "type": "WRITING_ONLY",
                    "title": "Improve writing quality",
                    "description": "Polish writing",
                    "status": "pending",
                    "actions": [{"agent": "writer", "task": "improve writing"}],
                },
            ]
        }
        with open(self.state_dir / "action_plan.yaml", "w") as f:
            yaml.dump(plan, f, default_flow_style=False, allow_unicode=True)


# ---------------------------------------------------------------------------
#  Mock fitz (PyMuPDF)
# ---------------------------------------------------------------------------

def _make_mock_fitz(latex_dir: Path):
    """Create a mock fitz module that produces tiny PNGs."""
    mock_fitz = MagicMock()

    mock_page = MagicMock()
    mock_pix = MagicMock()

    def save_png(path):
        Path(path).write_bytes(PNG_BYTES)

    mock_pix.save = save_png

    mock_page.get_pixmap.return_value = mock_pix

    mock_doc = MagicMock()
    mock_doc.__iter__ = lambda self: iter([mock_page])
    mock_doc.__enter__ = lambda self: self
    mock_doc.__exit__ = lambda self, *a: None
    mock_doc.close = MagicMock()

    mock_fitz.open.return_value = mock_doc
    return mock_fitz


# ---------------------------------------------------------------------------
#  Substantial main.tex content (passes _paper_has_substantial_content check)
# ---------------------------------------------------------------------------

MAIN_TEX = r"""\documentclass[sigplan,10pt]{acmart}
\begin{document}
\title{When Smaller Is Slower: Dimensional Collapse in Compressed LLMs}
\author{Test Author}
\begin{abstract}
""" + (
    "This paper investigates dimensional collapse in compressed large language "
    "models and its surprising impact on GPU execution performance. We discover "
    "that popular weight compression techniques such as quantization and pruning "
    "can inadvertently reduce the effective rank of weight matrices, leading to "
    "what we term dimensional collapse. Through systematic profiling across "
    "multiple GPU architectures, we demonstrate that this phenomenon triggers "
    "performance cliffs in hardware-optimized routines including Tensor Core "
    "operations, vectorized memory access, and L2 cache utilization. Our analysis "
    "reveals that models compressed beyond a critical threshold experience "
    "slowdowns that can negate the latency benefits of compression. "
    "We propose GAC, a geometry-aware compression strategy that preserves "
    "dimensional diversity during compression and maintains hardware efficiency."
) + r"""
\end{abstract}

\section{Introduction}
Large language models have achieved remarkable performance across diverse natural
language processing tasks, from machine translation to code generation. However,
deploying these models in production environments requires significant computational
resources. Model compression techniques, including quantization, pruning, and
knowledge distillation, have emerged as essential tools for reducing the memory
footprint and computational cost of large models. Despite their benefits, these
compression techniques can introduce subtle performance pathologies on modern GPU
hardware that are not well understood by the research community.

In this paper, we systematically study the phenomenon of dimensional collapse
in compressed language models and its impact on GPU execution efficiency. We find
that aggressive compression can reduce the effective rank of weight matrices,
leading to suboptimal utilization of hardware-optimized routines.

\section{Background}
Modern GPUs rely on Tensor Cores and wide vector units for efficient matrix
multiplication. These hardware units achieve peak throughput when operating on
matrices with specific alignment and dimensionality requirements. When weight
matrices lose rank diversity through compression, hardware utilization drops
significantly, potentially negating the intended speedup from model compression.
Understanding these hardware-software interactions is crucial for designing
compression strategies that maintain both model quality and execution efficiency.

\section{Methodology}
We profile compressed models across NVIDIA A100 and H100 GPUs, measuring Tensor
Core utilization, memory bandwidth, and L2 cache hit rates. Our experimental
setup includes multiple model architectures ranging from 7B to 70B parameters,
compressed using various quantization schemes including GPTQ, AWQ, and SqueezeLLM.
We measure end-to-end inference latency, per-layer execution time, and hardware
counter statistics to identify the root causes of performance degradation.

\section{Results}
Our experiments show that 4-bit quantized models suffer up to 2.3x slowdown
compared to FP16 baselines when dimensional collapse occurs. We identify four
primary mechanisms through which dimensional collapse degrades performance:
reduced Tensor Core utilization, inefficient vectorized memory access patterns,
increased L2 cache pressure, and suboptimal thread block scheduling. Our GAC
strategy addresses these issues by preserving geometric diversity during the
compression process, achieving comparable compression ratios with significantly
better hardware utilization and inference speed.

\section{Conclusion}
Dimensional collapse is a critical consideration for practical LLM deployment.
Our geometry-aware compression strategy addresses this by maintaining dimensional
diversity during compression while preserving model accuracy. Future work will
extend this analysis to emerging hardware architectures and alternative compression
paradigms including structured pruning and mixed-precision training approaches.

\bibliographystyle{ACM-Reference-Format}
\bibliography{references}
\end{document}
"""


# ---------------------------------------------------------------------------
#  Fixture: integration_project
# ---------------------------------------------------------------------------

@pytest.fixture
def integration_project(tmp_path):
    """Set up a complete temporary project and return (orchestrator, controller)."""
    # -- Directory tree --
    proj_dir = tmp_path / "projects" / "test_integ"
    agents_dir = proj_dir / "agents"
    agents_dir.mkdir(parents=True)

    latex_dir = tmp_path / "Latex"
    latex_dir.mkdir()
    figures_dir = latex_dir / "figures"
    figures_dir.mkdir()

    state_dir = tmp_path / "auto_research" / "state"
    state_dir.mkdir(parents=True)
    log_dir = tmp_path / "auto_research" / "logs"
    log_dir.mkdir(parents=True)

    # -- Config --
    config = {
        "code_dir": str(tmp_path),
        "latex_dir": "Latex",
        "figures_dir": "Latex/figures",
        "use_slurm": False,
        "paper_accept_threshold": 8,
        "venue_format": "acmart-sigplan",
    }
    (proj_dir / "config.yaml").write_text(yaml.dump(config))

    # -- Agent prompt files (one line each, with agent type marker) --
    agent_types = [
        "reviewer", "planner", "writer", "visualizer",
        "experimenter", "researcher", "meta_debugger", "coder",
    ]
    for at in agent_types:
        (agents_dir / f"{at}.prompt").write_text(
            f"[AGENT:{at}] You are the {at} agent.\n"
        )

    # -- Substantial main.tex --
    (latex_dir / "main.tex").write_text(MAIN_TEX)
    # References bib
    (latex_dir / "references.bib").write_text(
        "@article{test2025, title={Test}, author={A}, year={2025}}\n"
    )

    # -- MockController --
    controller = MockController(state_dir, latex_dir, review_score=7.0)
    mock_fitz = _make_mock_fitz(latex_dir)

    # -- Save original cwd --
    orig_cwd = os.getcwd()

    # -- Patches --
    patches = [
        patch("ark.orchestrator.ARK_ROOT", tmp_path),
        patch("ark.cli.ensure_project_symlinks", return_value=None),
        patch("subprocess.run", side_effect=controller.subprocess_run),
        patch("subprocess.Popen", side_effect=controller.subprocess_popen),
        patch.dict("sys.modules", {"fitz": mock_fitz}),
        patch("time.sleep", return_value=None),  # prevent any sleeps
    ]
    for p in patches:
        p.start()

    # -- Build orchestrator --
    from ark.orchestrator import Orchestrator
    orch = Orchestrator(
        project="test_integ",
        mode="paper",
        code_dir=str(tmp_path),
    )

    yield orch, controller

    # -- Teardown --
    for p in patches:
        p.stop()
    os.chdir(orig_cwd)


# ===========================================================================
#  Test cases
# ===========================================================================

class TestIntegration:
    """Integration tests for the full paper iteration pipeline."""

    @pytest.fixture(autouse=True)
    def _mock_telegram(self):
        """Prevent tests from sending real Telegram messages."""
        with patch("ark.telegram.TelegramConfig.is_configured", new_callable=lambda: property(lambda self: False)):
            yield

    def test_pip_installability(self):
        """Verify package can be imported."""
        import ark
        from ark.orchestrator import Orchestrator
        from ark.cli import main
        from ark.memory import SimpleMemory
        from ark.agents import AgentMixin
        from ark.compiler import CompilerMixin
        from ark.execution import ExecutionMixin
        from ark.pipeline import PipelineMixin

    def test_compile_latex_creates_pdf(self, integration_project):
        """compile_latex() should create main.pdf."""
        orch, controller = integration_project
        assert orch.compile_latex() is True
        assert (orch.latex_dir / "main.pdf").exists()

    def test_full_paper_iteration(self, integration_project):
        """Run one full paper iteration and verify all state files."""
        orch, controller = integration_project

        result = orch.run_paper_iteration()

        # Should continue (score 7 < threshold 8)
        assert result is True
        assert orch.iteration == 1

        # paper_state.yaml
        paper_state = orch.load_paper_state()
        assert paper_state["current_score"] == 7.0
        assert len(paper_state["reviews"]) == 1
        assert paper_state["reviews"][0]["score"] == 7.0

        # latest_review.md
        assert orch.latest_review_file.exists()
        review_content = orch.latest_review_file.read_text()
        assert "7" in review_content

        # action_plan.yaml
        assert orch.action_plan_file.exists()
        plan = orch._load_action_plan()
        assert len(plan.get("issues", [])) > 0

        # checkpoint.yaml
        assert orch.checkpoint_file.exists()
        checkpoint = orch.load_checkpoint()
        assert checkpoint["iteration"] == 1

        # Memory
        assert orch.memory.scores == [7.0]

        # Cost tracking
        assert len(orch._agent_stats) > 0
        for stat in orch._agent_stats:
            assert "agent_type" in stat
            assert "elapsed_seconds" in stat

        # Agent call sequence: should include key agents
        called = controller.agent_calls
        assert "reviewer" in called
        assert "planner" in called
        assert "writer" in called

    def test_paper_accepted_stops(self, tmp_path):
        """Score >= threshold should stop iteration (return False)."""
        proj_dir = tmp_path / "projects" / "test_accept"
        agents_dir = proj_dir / "agents"
        agents_dir.mkdir(parents=True)

        latex_dir = tmp_path / "Latex"
        latex_dir.mkdir()
        (latex_dir / "figures").mkdir()
        (latex_dir / "main.tex").write_text(MAIN_TEX)
        (latex_dir / "references.bib").write_text("")

        state_dir = tmp_path / "auto_research" / "state"
        state_dir.mkdir(parents=True)
        (tmp_path / "auto_research" / "logs").mkdir(parents=True)

        config = {
            "code_dir": str(tmp_path),
            "use_slurm": False,
            "paper_accept_threshold": 8,
            "venue_format": "acmart-sigplan",
        }
        (proj_dir / "config.yaml").write_text(yaml.dump(config))

        for at in ["reviewer", "planner", "writer", "visualizer",
                    "experimenter", "researcher", "meta_debugger", "coder"]:
            (agents_dir / f"{at}.prompt").write_text(f"[AGENT:{at}]\n")

        controller = MockController(state_dir, latex_dir, review_score=9.0)
        mock_fitz = _make_mock_fitz(latex_dir)
        orig_cwd = os.getcwd()

        patches = [
            patch("ark.orchestrator.ARK_ROOT", tmp_path),
            patch("ark.cli.ensure_project_symlinks", return_value=None),
            patch("subprocess.run", side_effect=controller.subprocess_run),
            patch("subprocess.Popen", side_effect=controller.subprocess_popen),
            patch.dict("sys.modules", {"fitz": mock_fitz}),
            patch("time.sleep", return_value=None),
        ]
        for p in patches:
            p.start()

        try:
            from ark.orchestrator import Orchestrator
            orch = Orchestrator(
                project="test_accept",
                mode="paper",
                code_dir=str(tmp_path),
            )
            result = orch.run_paper_iteration()

            # Score 9 >= threshold 8 → paper accepted, should stop
            assert result is False
            paper_state = orch.load_paper_state()
            assert paper_state["status"] == "accepted"
        finally:
            for p in patches:
                p.stop()
            os.chdir(orig_cwd)

    def test_score_zero_retries_reviewer(self, tmp_path):
        """When initial score is 0 but review exists, should retry reviewer."""
        proj_dir = tmp_path / "projects" / "test_retry"
        agents_dir = proj_dir / "agents"
        agents_dir.mkdir(parents=True)

        latex_dir = tmp_path / "Latex"
        latex_dir.mkdir()
        (latex_dir / "figures").mkdir()
        (latex_dir / "main.tex").write_text(MAIN_TEX)
        (latex_dir / "references.bib").write_text("")

        state_dir = tmp_path / "auto_research" / "state"
        state_dir.mkdir(parents=True)
        (tmp_path / "auto_research" / "logs").mkdir(parents=True)

        config = {
            "code_dir": str(tmp_path),
            "use_slurm": False,
            "paper_accept_threshold": 8,
            "venue_format": "acmart-sigplan",
        }
        (proj_dir / "config.yaml").write_text(yaml.dump(config))

        for at in ["reviewer", "planner", "writer", "visualizer",
                    "experimenter", "researcher", "meta_debugger", "coder"]:
            (agents_dir / f"{at}.prompt").write_text(f"[AGENT:{at}]\n")

        class RetryController(MockController):
            """First reviewer call returns no score, second returns 7.0."""
            def _agent_stdout(self, agent_type, prompt):
                if agent_type == "reviewer":
                    self._reviewer_call_count += 1
                    if self._reviewer_call_count == 1:
                        # First call: long output but no parseable score
                        return "The paper is well written and presents interesting findings. " * 10
                    else:
                        return ("Overall Score: 7.0/10\nReview report update complete\n"
                                "The review has been updated with the explicit score.\n"
                                "All sections have been evaluated thoroughly.\n")
                return super()._agent_stdout(agent_type, prompt)

            def _write_review(self):
                if self._reviewer_call_count == 1:
                    # No score in file either
                    (self.state_dir / "latest_review.md").write_text(
                        "# Review\nThe paper is well written. " * 20 + "\n"
                        "### M1. Need experiments\n### m1. Typos\n"
                    )
                else:
                    super()._write_review()

        controller = RetryController(state_dir, latex_dir, review_score=7.0)
        mock_fitz = _make_mock_fitz(latex_dir)
        orig_cwd = os.getcwd()

        patches = [
            patch("ark.orchestrator.ARK_ROOT", tmp_path),
            patch("ark.cli.ensure_project_symlinks", return_value=None),
            patch("subprocess.run", side_effect=controller.subprocess_run),
            patch("subprocess.Popen", side_effect=controller.subprocess_popen),
            patch.dict("sys.modules", {"fitz": mock_fitz}),
            patch("time.sleep", return_value=None),
        ]
        for p in patches:
            p.start()

        try:
            from ark.orchestrator import Orchestrator
            orch = Orchestrator(
                project="test_retry",
                mode="paper",
                code_dir=str(tmp_path),
            )
            result = orch.run_paper_iteration()

            # Should have called reviewer twice (initial + retry)
            reviewer_calls = [c for c in controller.agent_calls if c == "reviewer"]
            assert len(reviewer_calls) >= 2

            # Score should be 7.0 after retry
            paper_state = orch.load_paper_state()
            assert paper_state["current_score"] == 7.0
        finally:
            for p in patches:
                p.stop()
            os.chdir(orig_cwd)

    def test_figure_phase_runs(self, integration_project):
        """Verify figure phase runs during iteration (figure_fixer only if images available)."""
        orch, controller = integration_project
        orch.run_paper_iteration()
        # Figure phase runs but figure_fixer only called if pdf_to_images returns page images.
        # In test environment without fitz, it skips figure_fixer gracefully.
        assert "reviewer" in controller.agent_calls  # Pipeline still runs

    def test_memory_updated(self, integration_project):
        """After iteration, memory should record the score."""
        orch, controller = integration_project
        orch.run_paper_iteration()
        assert orch.memory.scores == [7.0]
        assert orch.memory.best_score == 7.0

    def test_cost_tracking(self, integration_project):
        """After iteration, _agent_stats should be populated."""
        orch, controller = integration_project
        orch.run_paper_iteration()
        assert len(orch._agent_stats) > 0
        for stat in orch._agent_stats:
            assert "agent_type" in stat
            assert "elapsed_seconds" in stat
            assert "timestamp" in stat

    def test_cost_tracking_token_fields(self, integration_project):
        """When claude returns JSON, _agent_stats and cost_report.yaml carry
        real token + USD aggregates parsed from the envelope."""
        import yaml as _yaml
        orch, controller = integration_project
        controller.json_mode = True
        orch.run_paper_iteration()

        # Per-call stats include the new token/cost fields
        assert orch._agent_stats, "expected at least one agent call"
        for stat in orch._agent_stats:
            for key in ("input_tokens", "output_tokens",
                        "cache_read_tokens", "cache_creation_tokens",
                        "cost_usd", "model"):
                assert key in stat, f"missing {key} in stat"
            # The mock envelope sets these to fixed values for non-error calls
            if not stat.get("error"):
                assert stat["input_tokens"] == 100
                assert stat["output_tokens"] == 50
                assert stat["cache_read_tokens"] == 800
                assert stat["cache_creation_tokens"] == 200
                assert stat["cost_usd"] == controller.json_cost_per_call
                assert stat["model"] == "claude-opus-4-7[1m]"

        # cost_report.yaml is written live and aggregates correctly
        report_path = orch.state_dir / "cost_report.yaml"
        assert report_path.exists(), "live cost report should exist after run"
        report = _yaml.safe_load(report_path.read_text())
        n_calls = report["total_agent_calls"]
        assert n_calls > 0
        assert report["total_cost_usd"] == round(
            n_calls * controller.json_cost_per_call, 6
        )
        assert report["total_input_tokens"] == n_calls * 100
        assert report["total_output_tokens"] == n_calls * 50
        assert report["total_cache_read_tokens"] == n_calls * 800
        assert report["total_cache_creation_tokens"] == n_calls * 200
        # per-agent buckets carry the same fields
        for agent_name, bucket in report["per_agent"].items():
            assert "total_cost_usd" in bucket
            assert "total_input_tokens" in bucket
            assert bucket["total_cost_usd"] >= 0

    def test_cost_tracking_malformed_json(self, integration_project):
        """When claude stdout is not valid JSON, agents fall back to plain
        text without crashing and stats append with zero cost fields."""
        orch, controller = integration_project
        # json_mode stays False — mock returns plain text, which should
        # fail _parse_claude_json and trigger the fallback path
        orch.run_paper_iteration()
        assert orch._agent_stats, "expected at least one agent call"
        for stat in orch._agent_stats:
            # Fallback path leaves cost fields at their zero defaults
            assert stat.get("cost_usd", 0) == 0
            assert stat.get("input_tokens", 0) == 0
            assert stat.get("output_tokens", 0) == 0
        # Live cost report still written even with zero costs
        assert (orch.state_dir / "cost_report.yaml").exists()
