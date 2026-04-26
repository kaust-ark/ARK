import pytest
import os
import struct
import zlib
import json
import yaml
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

def pytest_addoption(parser):
    parser.addoption(
        "--gcloud-path",
        action="store",
        default=None,
        help="Path to gcloud SDK bin directory (e.g. /path/to/google-cloud-sdk/bin)"
    )

@pytest.fixture(scope="session", autouse=True)
def _setup_gcloud_path(request):
    """Ensure gcloud is in PATH if provided via CLI or environment."""
    import shutil
    if shutil.which("gcloud"):
        return
        
    path = request.config.getoption("--gcloud-path") or os.environ.get("ARK_GCLOUD_PATH")
    if path:
        if os.path.exists(path):
            os.environ["PATH"] = f"{path}:{os.environ.get('PATH', '')}"
        else:
            print(f"\nWARNING: Provided gcloud path does not exist: {path}")

@pytest.fixture(autouse=True)
def _reset_memory_singleton():
    """SimpleMemory uses a global _instance to ensure singleton behaviour across
    the orchestrator and its mixins. For tests to be isolated, we must clear
    this state before every test case.
    """
    from ark import memory
    memory._memory = None
    yield

@pytest.fixture
def tmp_state_dir(tmp_path):
    d = tmp_path / "auto_research" / "state"
    d.mkdir(parents=True)
    return d

@pytest.fixture
def tmp_figures_dir(tmp_path):
    d = tmp_path / "paper" / "figures"
    d.mkdir(parents=True)
    return d

# ---------------------------------------------------------------------------
#  Integration Test Mocks & Fixtures
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

class MockController:
    """Manages all subprocess mock behaviour for the integration tests."""

    def __init__(self, state_dir: Path, latex_dir: Path, review_score: float = 7.0):
        self.state_dir = state_dir
        self.latex_dir = latex_dir
        self.review_score = review_score
        self.agent_calls: list[str] = []
        self._reviewer_call_count = 0
        self.json_mode = False
        self.json_cost_per_call = 0.025

    def subprocess_run(self, cmd, **kwargs):
        if not cmd: return self._ok("")
        exe = cmd[0] if isinstance(cmd, (list, tuple)) else cmd
        if exe == "pdflatex":
            pdf = kwargs.get("cwd", self.latex_dir) / "main.pdf" if isinstance(kwargs.get("cwd"), Path) else Path(kwargs.get("cwd", str(self.latex_dir))) / "main.pdf"
            pdf.write_bytes(b"%PDF-1.4 fake")
            return self._ok("")
        if exe == "bibtex": return self._ok("")
        if exe == "git": return self._handle_git(cmd, **kwargs)
        if exe in ("mail", "squeue", "python", "bash"): return self._ok("")
        if exe == "claude": return self._ok("claude-code 1.0.0")
        return self._ok("")

    def subprocess_popen(self, cmd, **kwargs):
        prompt = " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        agent_type = self._detect_agent(prompt)
        self.agent_calls.append(agent_type)
        stdout_text = self._agent_stdout(agent_type, prompt)

        if agent_type == "reviewer":
            self._write_review()
        elif agent_type == "planner":
            self._write_action_plan()

        if self.json_mode:
            envelope = {
                "type": "result", "subtype": "success", "is_error": False, "result": stdout_text,
                "duration_ms": 1500, "duration_api_ms": 1400, "total_cost_usd": self.json_cost_per_call,
                "usage": {"input_tokens": 100, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 800, "output_tokens": 50},
                "modelUsage": {"claude-opus-4-7[1m]": {"inputTokens": 100, "outputTokens": 50, "cacheReadInputTokens": 800, "cacheCreationInputTokens": 200, "costUSD": self.json_cost_per_call}},
            }
            stdout_text = json.dumps(envelope)

        proc = MagicMock()
        proc.communicate.return_value = (stdout_text, "")
        proc.returncode = 0
        proc.stdout = BytesIO(stdout_text.encode())
        proc.stderr = BytesIO(b"")
        return proc

    def _ok(self, stdout, returncode=0):
        m = MagicMock()
        m.stdout = stdout
        m.stderr = ""
        m.returncode = returncode
        return m

    def _handle_git(self, cmd, **kwargs):
        subcmd = cmd[1] if len(cmd) > 1 else ""
        if subcmd == "status": return self._ok("")
        if subcmd == "diff":
            if "--name-only" in cmd: return self._ok("")
            if "--numstat" in cmd: return self._ok("10\t2\tLatex/main.tex")
            if "--stat" in cmd: return self._ok(" Latex/main.tex | 10 +++++++---")
            return self._ok("")
        if subcmd == "commit": return self._ok("", returncode=1)
        return self._ok("")

    def _detect_agent(self, prompt: str) -> str:
        agent_types = ["visualizer", "reviewer", "planner", "writer", "experimenter", "researcher", "meta_debugger", "coder"]
        for at in agent_types:
            if f"[AGENT:{at}]" in prompt or f"{at}.prompt" in prompt: return at
        prompt_lower = prompt.lower()
        if "review" in prompt_lower: return "reviewer"
        if "planner" in prompt_lower or "action_plan" in prompt_lower: return "planner"
        if "writer" in prompt_lower or "writing" in prompt_lower: return "writer"
        if "figure_config.json" in prompt_lower or "visualizer" in prompt_lower: return "visualizer"
        return "unknown"

    def _agent_stdout(self, agent_type: str, prompt: str) -> str:
        filler = ("\nThe agent has completed the requested task successfully. "
                  "All files have been updated according to the instructions provided. "
                  "No errors were encountered during execution.\n")
        if agent_type == "reviewer":
            self._reviewer_call_count += 1
            return f"Overall Score: {self.review_score}/10\nReview report saved to auto_research/state/latest_review.md\n{filler}"
        if agent_type == "planner": return f"Generated action_plan.yaml containing all issues to be addressed\n{filler}"
        if agent_type == "writer": return f"Updated main.tex modified Introduction and Results sections\n{filler}"
        if agent_type == "visualizer": return f"FIGURES_OK all figure quality checks passed correct dimensions clear fonts\n{filler}"
        if agent_type == "meta_debugger": return f"CONTINUE system status normal no issues requiring repair found\n{filler}"
        return f"done task completed successfully\n{filler}"

    def _write_review(self):
        review = (f"# Review Report\n\nOverall Score: {self.review_score}/10\n\n"
                  f"## Major Issues\n### M1. Need more experiments\n### M2. Improve writing\n\n"
                  f"## Minor Issues\n### m1. Fix typos\n")
        (self.state_dir / "latest_review.md").write_text(review)

    def _write_action_plan(self):
        plan = {"issues": [{"id": "M1", "type": "WRITING_ONLY", "title": "Need more experiments", "status": "pending", "actions": [{"agent": "writer", "task": "update"}]},
                           {"id": "M2", "type": "WRITING_ONLY", "title": "Improve writing", "status": "pending", "actions": [{"agent": "writer", "task": "polish"}]}]}
        with open(self.state_dir / "action_plan.yaml", "w") as f:
            yaml.dump(plan, f, default_flow_style=False, allow_unicode=True)

def _make_mock_fitz():
    mock_fitz = MagicMock()
    mock_page = MagicMock()
    mock_pix = MagicMock()
    mock_pix.save = lambda path: Path(path).write_bytes(PNG_BYTES)
    mock_page.get_pixmap.return_value = mock_pix
    mock_doc = MagicMock()
    mock_doc.__iter__ = lambda self: iter([mock_page])
    mock_doc.__enter__ = lambda self: self
    mock_doc.__exit__ = lambda self, *a: None
    mock_fitz.open.return_value = mock_doc
    return mock_fitz

MAIN_TEX = r"""\documentclass[sigplan,10pt]{acmart}
\begin{document}
\title{Test Paper}
\begin{abstract}
This is a substantial abstract that should pass the length check. It needs to be long enough to be considered a real paper content by the orchestrator's heuristics.
\end{abstract}
\section{Introduction}
Intro text.
\section{Conclusion}
Conclusion text.
\bibliographystyle{ACM-Reference-Format}
\bibliography{references}
\end{document}
"""

@pytest.fixture
def mock_integration_project_factory(tmp_path):
    """Factory fixture to create a complete temporary project with custom settings."""
    patches = []
    orig_cwd = os.getcwd()

    def _create(project_name="test_integ", review_score=7.0, controller_cls=MockController):
        proj_dir = tmp_path / "projects" / project_name
        agents_dir = proj_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)

        latex_dir = tmp_path / "Latex"
        latex_dir.mkdir(exist_ok=True)
        (latex_dir / "figures").mkdir(exist_ok=True)
        state_dir = tmp_path / "auto_research" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (tmp_path / "auto_research" / "logs").mkdir(parents=True, exist_ok=True)

        config = {"code_dir": str(tmp_path), "latex_dir": "Latex", "figures_dir": "Latex/figures", "use_slurm": False, "paper_accept_threshold": 8, "venue_format": "acmart-sigplan"}
        (proj_dir / "config.yaml").write_text(yaml.dump(config))

        for at in ["reviewer", "planner", "writer", "visualizer", "experimenter", "researcher", "meta_debugger", "coder"]:
            (agents_dir / f"{at}.prompt").write_text(f"[AGENT:{at}]\n")

        (latex_dir / "main.tex").write_text(MAIN_TEX)
        (latex_dir / "references.bib").write_text("@article{t, title={T}, author={A}, year={2025}}\n")

        controller = controller_cls(state_dir, latex_dir, review_score=review_score)
        mock_fitz = _make_mock_fitz()

        p_list = [
            patch("ark.orchestrator.core.ARK_ROOT", tmp_path),
            patch("ark.cli.ensure_project_symlinks", return_value=None),
            patch("subprocess.run", side_effect=controller.subprocess_run),
            patch("subprocess.Popen", side_effect=controller.subprocess_popen),
            patch.dict("sys.modules", {"fitz": mock_fitz}),
            patch("time.sleep", return_value=None),
        ]
        for p in p_list:
            p.start()
            patches.append(p)

        from ark.orchestrator import Orchestrator
        orch = Orchestrator(project=project_name, mode="paper", code_dir=str(tmp_path))
        return orch, controller

    yield _create

    for p in patches:
        p.stop()
    os.chdir(orig_cwd)

@pytest.fixture
def mock_integration_project(mock_integration_project_factory):
    """Set up a complete temporary project and return (orchestrator, controller)."""
    return mock_integration_project_factory()
