"""Scientific operations exposed to the native Idea2Paper DSH bundle.

The native agent chooses which operation to call. These operations bind external
side effects and delivery decisions to the project's durable scientific record.
"""

from __future__ import annotations

import ast
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from html import unescape
from urllib.parse import quote

import yaml

from ark import (
    citation,
    deep_research,
    delivery_contract,
    figure_manifest,
    literature_evidence,
    measurement_table,
    review,
)
from ark.compute import native_experiment
from ark.execution import ExecutionMixin
from ark.latex.compiler import CompilerMixin
from ark.latex.utils import body_page_boundary, extract_latex_errors, parse_overfull_warnings
from ark.orchestrator.state import _atomic_write_text


def _json(path, obj):
    _atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def _load(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(args, key, *, limit=4000):
    result = args.get(key)
    if not isinstance(result, str) or not result.strip() or len(result) > limit:
        raise ValueError(f"Expected a nonempty {key} up to {limit} characters.")
    return result.strip()


def _asset_dir(name):
    packaged = Path(__file__).resolve().parents[1] / "bundle_assets" / name
    return packaged if packaged.is_dir() else Path(__file__).resolve().parents[2] / name


def install_skills(root):
    """Install before the first turn, including on a writing-only resume."""
    directory = _asset_dir("skills") / "builtin"
    if not directory.is_dir():
        raise RuntimeError("Installed bundle is missing its built-in skills.")
    for skill in sorted(directory.glob("*/SKILL.md")):
        dest = root / ".claude/skills" / skill.parent.name
        if not dest.exists():
            shutil.copytree(skill.parent, dest)


_PAPER_EXTENSIONS = {".tex", ".sty", ".bst", ".bib", ".cls", ".png", ".jpg", ".jpeg", ".pdf"}
_PDFLATEX = (
    "pdflatex", "-file-line-error", "-recorder", "-no-shell-escape",
    "-halt-on-error", "-interaction=nonstopmode", "main.tex",
)


def _paper_inputs(paper):
    if paper.resolve() != paper:
        raise ValueError("Paper assets must be regular files inside paper/.")
    for path in sorted(paper.rglob("*")):
        if path == paper / "main.pdf":
            continue
        if path.is_symlink() and (path.suffix.lower() in _PAPER_EXTENSIONS or path.is_dir()):
            raise ValueError("Paper assets must be regular files inside paper/.")
        if path.suffix.lower() in _PAPER_EXTENSIONS and path.is_file():
            if path.resolve() != path:
                raise ValueError("Paper assets must be regular files inside paper/.")
            yield path


def _paper_sources(paper):
    digest = hashlib.sha256()
    for path in _paper_inputs(paper):
        digest.update(str(path.relative_to(paper)).encode() + b"\0" + bytes.fromhex(_sha(path)))
    return digest.hexdigest()


def _stage_tex(paper, staging):
    for path in _paper_inputs(paper):
        target = staging / path.relative_to(paper)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TEXMFCNF") if k in os.environ}
    env.update(
        HOME=str(staging), openin_any="p", openout_any="p", shell_escape="f", SOURCE_DATE_EPOCH="1609459200"
    )
    return env


def _manuscript_first_pass(paper, content):
    with tempfile.TemporaryDirectory(prefix="idea2paper-edit-") as tmp:
        staging = Path(tmp)
        env = _stage_tex(paper, staging)
        (staging / "main.tex").write_bytes(content)
        run = subprocess.run(
            _PDFLATEX, cwd=staging, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=90
        )
        if run.returncode:
            log = staging / "main.log"
            if log.is_file():
                return extract_latex_errors(log)[:2000]
            return (run.stdout + run.stderr).decode(errors="replace").strip()[-2000:] or "pdflatex failed."
        pdf = staging / "main.pdf"
        if not pdf.is_file() or not pdf.stat().st_size:
            return "pdflatex produced no manuscript pages."
        return None


class NativeProject(CompilerMixin):
    def __init__(self, root, *, host=None):
        self.root = Path(root).resolve()
        self.state_dir = self.root / "auto_research/state"
        self.latex_dir = self.root / "paper"
        self.figures_dir = self.latex_dir / "figures"
        self.host = host
        self.config = {}
        self.path = self.state_dir / "native_project.json"
        for path in (
            self.state_dir,
            self.latex_dir,
            self.figures_dir,
            self.root / "results",
            self.root / "scripts",
            self.root / "sources",
            self.root / ".claude/skills",
        ):
            if path.resolve() != path:
                raise ValueError("Project directories must be inside the project without symbolic links.")
        self.state = _load(self.path, {})
        self.snapshot = None
        self.last_status = None
        self.review_receipts = []
        cfg_path = self.root / "config.yaml"
        if cfg_path.exists():
            self.config = yaml.safe_load(cfg_path.read_text()) or {}

    def log(self, msg, level="INFO"):
        if self.host:
            self.host.log(str(msg), level)
        else:
            print(msg, file=sys.stderr, flush=True)

    def save(self):
        _json(self.path, self.state)

    def event(self, phase, action, observation, **attrs):
        row = {
            "id": uuid.uuid4().hex,
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "action": action,
            "observation": str(observation)[:1500],
            **attrs,
        }
        with (self.state_dir / "research_events.jsonl").open("a", encoding="utf-8") as events:
            events.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            events.flush()
            os.fsync(events.fileno())
        if self.host:
            self.host.log(f"{action}: {observation}", "INFO")
        return row

    def _initialize(self, args):
        idea = self.config.get("idea") or args.get("idea")
        idea_file = self.state_dir / "idea.md"
        if not idea and idea_file.is_file():
            idea = idea_file.read_text()
        if not isinstance(idea, str) or not idea.strip():
            raise ValueError("Start a new project with a research idea.")
        for path in (self.state_dir, self.figures_dir, self.root / "results", self.root / "scripts"):
            path.mkdir(parents=True, exist_ok=True)
        if not self.config:
            google = bool(deep_research.get_gemini_api_key())
            self.config = {
                "project": self.root.name,
                "idea": idea.strip(),
                "venue_format": "icml",
                "venue_pages": 4,
                "layout_mode": "strict",
                "latex_dir": "paper",
                "skip_deep_research": not google,
                "figure_generation": "nano_banana" if google else "none",
                "deep_research_backend": "gemini" if google else "",
                "experiment_compute_backend": {"type": "local"},
            }
            _atomic_write_text(self.root / "config.yaml", yaml.safe_dump(self.config, allow_unicode=True))
        if not idea_file.exists():
            _atomic_write_text(idea_file, idea.strip())
        if not (self.latex_dir / "main.tex").is_file():
            venue = self.config.get("venue_format", "icml")
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", venue):
                raise ValueError("Invalid venue template.")
            source = _asset_dir("venue_templates") / venue
            if not source.is_dir():
                raise ValueError(
                    f"The installed distribution does not contain the requested {venue} template."
                )
            shutil.copytree(source, self.latex_dir, dirs_exist_ok=True)
        install_skills(self.root)
        self.state = {"version": 1, "state": "running", "started": datetime.now(timezone.utc).isoformat()}
        self.save()
        self.event("research", "project_started", "Project opened with persistent research owner.")
        self._initial_gate()

    def _initial_gate(self):
        if self.state.get("ethics_completed"):
            return
        path = self.state_dir / "ethical_review.json"
        if self.host:
            allowed = self.host._run_ethical_review()
        else:
            from ark.ethical_review import review_idea

            outcome = _load(path)
            if not outcome:
                outcome = review_idea(
                    self.config["idea"], self.config.get("model") or os.getenv("MAIN_MODEL_NAME", "")
                )
                _json(path, outcome)
            allowed = outcome.get("verdict", "proceed") == "proceed"
        self.state["ethics_completed"] = bool(allowed)
        if not allowed:
            self.state.update(state="paused", pause_reason="ethics")
            self.save()
            raise RuntimeError(
                "Research paused at the existing idea review gate. Inspect ethical_review.json."
            )
        self.save()

    def inspect_project(self, args):
        """Read-only status for CLI observers; never starts or steers a project."""
        if not self.state:
            return {"state": "new", "delivered": False}
        return self._status(args, inspect=True)

    def project_status(self, args):
        return self._status(args, inspect=False)

    def _status(self, args, *, inspect):
        if not self.state:
            self._initialize(args)
        elif not inspect and self.state.get("state") == "running" and not self.state.get("ethics_completed"):
            self._initial_gate()
        project_state = self.state["state"]
        if project_state == "delivered":
            try:
                self._check_compiled()
                current = self._snapshot()
                verdict = _load(self.state_dir / "native_review.json", {})
                if not (
                    current["revision"] == self.state.get("delivery_revision")
                    and review.record_is_current(current, verdict)
                    and verdict.get("deliverable")
                ):
                    raise ValueError("Review is stale")
            except (FileNotFoundError, ValueError):
                project_state = "running"
                if not inspect:
                    self.state["state"] = project_state
                    self.save()
                    self.event("review", "revision_reopened", "Project inputs changed since delivery.")
        if self.host and not inspect:
            self.host._poll_control()
        data = {
            "state": project_state,
            "delivered": project_state == "delivered",
            "pause_reason": self.state.get("pause_reason"),
            "idea": self.config.get("idea") or (self.state_dir / "idea.md").read_text(),
            "review_attempts": len(self.state.get("review_attempts", {})),
            "max_review_attempts": self.config.get("max_review_attempts", 8),
            "required": {
                "google_research": self.config.get("skip_deep_research") is False,
                "concept_image": self.config.get("figure_generation") == "nano_banana",
                "body_pages": self.config.get("venue_pages", 4),
                "venue": self.config.get("venue_format", ""),
            },
            "capabilities": {
                "google": bool(deep_research.get_gemini_api_key()),
                "experiment": (self.config.get("experiment_compute_backend") or {}).get("type", "local"),
            },
            "owner_brief": (
                "Use the same session for planning, measurements, writing and repair. Read the venue skeleton and project skills. "
                "Investigate primary literature, freeze paired controls, run a Python experiment, inspect measurements, and generate its plots. "
                "Ground related work in the verified research report and consult the original sources. "
                "Use render_results_table to include the exact worker values in per-condition tables. "
                "Show the paired measurements with their conditions, units and denominators so a reader can reproduce the central comparison. "
                "Use the venue's body-page budget for evidence, uncertainty, interpretation and limitations; keep references and a substantive linked appendix after the body. "
                "Compile, verify citations, request an independent review, address consequential findings, then deliver. "
                "Call project tools when needed. Prefer reanalysis to a duplicate experiment. A negative result is valid. "
                "Report missing evidence rather than claiming success. Scientific scripts print one JSON object of measurements to stdout. "
                "A plotting script reads /study/results.json and saves the requested figure under /output/."
            ),
            "skills": [
                str(p.relative_to(self.root))
                for p in sorted((self.root / ".claude/skills").glob("*/SKILL.md"))
            ],
            "commands": [] if inspect else self._commands(),
        }
        for key in (
            "plan",
            "experiment",
            "research",
            "concept",
            "compiled_pdf_sha256",
            "compile",
            "delivery_revision",
            "plots",
            "tables",
        ):
            if key in self.state:
                data[key] = self.state[key]
        if self.state.get("compile") and not inspect:
            data["manuscript_issues"] = self.state["compile"].get("manuscript_issues", [])
        last_review = _load(self.state_dir / "native_review.json")
        if last_review:
            data["review"] = {
                "revision": last_review["revision"],
                "verdict": last_review["verdict"],
                "reviewed_pdf_matches_last_build": bool(
                    self.state.get("compiled_pdf_sha256")
                    and self.state["compiled_pdf_sha256"] == last_review.get("paper_pdf_sha256")
                ),
                "summary": last_review.get("summary", ""),
                "issues": last_review.get("issues", []),
            }
        audit = _load(self.state_dir / "native_citations.json")
        if audit:
            data["citations"] = {
                "verified": audit["verified"],
                "unconfirmed": audit["unconfirmed"],
                "sources": [item["source"] for item in audit["checked"].values() if item.get("source")],
            }
        if self.host and getattr(self.host, "_stop_requested", False):
            data.update(state="stopped", delivered=False)
        elif self.host and getattr(self.host, "_paused", False):
            data.update(state="paused", delivered=False)
        return data

    def stop(self, args):
        if self.state.get("state") != "stopped":
            self.state["state_before_stop"] = self.state.get("state", "running")
            self.state["state"] = "stopped"
            self.save()
            self.event("work", "stopped", "User stopped this project.", status="stopped")
        native_experiment.cancel(self.root)
        return {"state": "stopped"}

    def continue_project(self, args):
        status = self.state.get("state")
        if status == "stopped":
            native_experiment.cancel(self.root)
            if record := self._measurement_receipt():
                self.state["experiment"] = record
            self.state["state"] = self.state.pop("state_before_stop", "running")
        if self.state.get("state") == "paused" and self.state.get("pause_reason") in {"quota", "owner_error"}:
            self.state.update(state="running")
            self.state.pop("pause_reason", None)
        if self.state.get("state") != status:
            self.save()
        return self.project_status(args)

    def _measurement_receipt(self):
        plan = self.state.get("plan")
        digest = self.state.get("sealed_script")
        return native_experiment.measurement_receipt(self.root, plan, digest) if plan and digest else None

    def _commands(self):
        if not self.host:
            return []
        path = self.state_dir / "user_instructions.yaml"
        preferences = yaml.safe_load(path.read_text()) if path.exists() else {}
        entries = (preferences or {}).get("instructions", [])
        pending = []
        admitted = self.state.get("acknowledged", [])
        for key, text in list(getattr(self.host, "_native_commands", {}).items()):
            if str(key) in admitted:
                self.host.cp.ack_command(str(key))
                self.host._native_commands.pop(key, None)
            else:
                pending.append({"id": str(key), "text": text})
        for item in entries:
            if not isinstance(item, dict):
                continue
            message = item.get("message") or item.get("instruction")
            if message:
                identifier = hashlib.sha256(
                    json.dumps(item, sort_keys=True, default=str).encode()
                ).hexdigest()
                if identifier not in self.state.get("acknowledged", []):
                    pending.append({"id": identifier, "text": message})
        return pending

    def ack_command(self, args):
        identifier = _text(args, "id", limit=100)
        if identifier not in self.state.get("acknowledged", []):
            self.state.setdefault("acknowledged", []).append(identifier)
            self.save()
        if self.host and identifier in getattr(self.host, "_native_commands", {}):
            self.host.cp.ack_command(identifier)
            self.host._native_commands.pop(identifier, None)
        return {"acknowledged": identifier}

    def project_event(self, args):
        sid, seq = str(args.get("session_id", ""))[:100], args.get("seq")
        if sid and type(seq) is int:
            seqs = self.state.setdefault("session_seq", {})
            if seq <= seqs.get(sid, -1):
                return {"duplicate": True}
            seqs[sid] = seq
        kind = str(args.get("kind", "agent"))[:80]
        owner_fallback = kind == "failure" and args.get("role") == "owner" and seq is None
        if owner_fallback and self.state.get("state") != "running":
            # A durable turn/end, gate, delivery or Stop already owns the outcome.
            return {"recorded": False}
        if (kind == "turn/end" or owner_fallback) and not args.get("replayed"):
            from ark.engines.dsh_driver import classify

            code, detail = classify(args.get("reason"))
            if code == "DSH.QuotaExceeded" or any(
                token in (detail or "").casefold()
                for token in ("insufficient balance", "insufficient_balance", "insufficient_quota")
            ):
                kind = "quota"
            elif args.get("role") == "owner" and (args.get("reason") or {}).get("kind") == "error":
                kind = "failure"
        if kind == "quota" and self.state.get("state") == "running":
            self.state.update(state="paused", pause_reason="quota")
        elif kind == "failure" and args.get("role") == "owner" and not args.get("replayed") and self.state.get("state") == "running":
            self.state.update(state="paused", pause_reason="owner_error")
        usage = args.get("usage")
        if isinstance(usage, dict) and sid:
            totals = self.state.setdefault("token_usage", {})
            for name, count in usage.items():
                if isinstance(count, (float, int)) and count >= 0:
                    totals[name] = totals.get(name, 0) + count
        if kind in {"turn/end", "turn_end", "tool/call", "tool/result", "failure", "quota", "stopped"}:
            # Detailed tool output can contain manuscript text and user credentials.
            title = (
                "Provider quota reached. Continue after checking the account."
                if kind == "quota"
                else str(args.get("title") or kind)[:180]
            )
            status = {
                "tool/call": "running",
                "quota": "quota_paused",
                "stopped": "stopped",
                "failure": "failed",
            }.get(kind)
            if kind == "tool/result":
                status = "failed" if "failed" in title else "completed"
            self.event(
                "review" if args.get("role") == "reviewer" else "work",
                kind,
                title,
                **(
                    {"id": hashlib.sha256(f"{sid}/{seq}".encode()).hexdigest()}
                    if sid and type(seq) is int
                    else {}
                ),
                **({"status": status} if status else {}),
            )
        if self.host and callable(getattr(self.host, "_record_native_event", None)):
            self.host._record_native_event({**args, "kind": "turn/end"} if owner_fallback and kind == "quota" else args)
        self.save()
        return {"recorded": True}

    def research(self, args):
        key = deep_research.get_gemini_api_key()
        status = deep_research.research_report_status(self.state_dir)
        if status != "verified" and not key:
            raise ValueError("Google Deep Research requires the project's Gemini API key.")
        if status != "verified":
            result = deep_research.run_deep_research(self.config, self.state_dir, args.get("query"), key)
            if not result or deep_research.research_report_status(self.state_dir) != "verified":
                raise RuntimeError(
                    "Deep Research did not return a verified report. Check the saved interaction before continuing."
                )
        receipt = _load(self.state_dir / "deep_research_receipt.json")
        if self.state.get("research") != receipt["interaction_id"]:
            self.state["research"] = receipt["interaction_id"]
            self.save()
            self.event(
                "research",
                "literature_ready",
                "Google Deep Research report verified.",
                artifact="auto_research/state/deep_research.md",
            )
        report = (self.state_dir / "deep_research.md").read_text()
        return {
            "path": "auto_research/state/deep_research.md",
            "report": report[:28000],
            "total_chars": len(report),
            "next_offset": 28000 if len(report) > 28000 else None,
            "interaction_id": receipt["interaction_id"],
        }

    def plan_experiment(self, args):
        if (
            self.config.get("skip_deep_research") is False
            and deep_research.research_report_status(self.state_dir) != "verified"
        ):
            raise ValueError("Obtain a verified Deep Research report before declaring this experiment.")
        plan = {
            key: _text(args, key)
            for key in ("question", "baseline", "comparison", "metric", "script")
        }
        if "result_path" in args and "expected" in args and args["result_path"] != args["expected"]:
            raise ValueError("Conflicting output filenames in result_path and legacy expected.")
        plan["expected"] = _text(args, "result_path" if "result_path" in args else "expected")
        controls = args.get("controls")
        if (
            not isinstance(controls, list)
            or not 1 <= len(controls) <= 24
            or not all(isinstance(x, str) and 0 < len(x.strip()) <= 1000 for x in controls)
        ):
            raise ValueError("Declare the experimental controls as a list of strings.")
        plan["controls"] = controls
        if not re.fullmatch(r"scripts/[\w-]+\.py", plan["script"], re.ASCII):
            raise ValueError('script must be one Python file path such as "scripts/experiment.py".')
        if not re.fullmatch(r"results/[\w-]+\.json", plan["expected"], re.ASCII):
            raise ValueError('result_path must be one output file path such as "results/measurements.json".')
        existing = self._measurement_receipt() or self.state.get("experiment")
        pending = self.state.get("pending_plan_change", {})
        if pending.get("plan") == plan:
            return {"approved": False, "requested_change": pending["request"]}
        if self.state.get("plan") == plan and (existing or {}).get("status") not in {"failed", "cancelled"}:
            return plan
        if existing and existing.get("status") not in {"completed", "failed", "cancelled"}:
            raise ValueError("Reconcile the active experiment before replacing its plan.")
        if self.state.get("plan") and self.state.get("sealed_script") and not existing:
            raise ValueError("Reconcile the previously submitted experiment first.")
        if (
            existing
            and existing.get("status") == "completed"
            and plan["expected"] == self.state["plan"]["expected"]
        ):
            raise ValueError("Choose a distinct result filename to preserve prior measurements.")
        _atomic_write_text(self.state_dir / "experiment_plan.yaml", yaml.safe_dump(plan, allow_unicode=True))
        if self.host:
            adjustment = self.host._experiment_approval_gate(
                yaml.safe_dump(plan, sort_keys=False, allow_unicode=True)
            )
            if adjustment:
                self.state["pending_plan_change"] = {"plan": plan, "request": adjustment}
                self.save()
                return {"approved": False, "requested_change": adjustment}
        self.state.pop("pending_plan_change", None)
        self.state.update(plan=plan)
        self.state.pop("sealed_script", None)
        self.state.pop("experiment", None)
        self.save()
        self.event(
            "experiments",
            "plan_declared",
            plan["question"],
            artifact="auto_research/state/experiment_plan.yaml",
        )
        return plan

    def _execute(self, args, *, plot=False):
        if self.state.get("pending_plan_change"):
            raise ValueError("Revise the experiment plan to address the requested change before execution.")
        if not (plan := self.state.get("plan")):
            raise ValueError("Declare the question, baseline, metric and paired controls before executing.")
        script = plan["script"]
        filename = _text(args, "filename") if plot else None
        options = {}
        if not plot:
            source = (self.root / script).resolve()
            if source.parent != self.root / "scripts":
                raise ValueError("Invalid experiment script.")
            code_hash = _sha(source)
            if self.state.get("sealed_script") not in (None, code_hash):
                raise ValueError("The submitted code changed. Restore the original script before polling.")
            self.state["sealed_script"] = code_hash
            self.save()
        else:
            receipt = self.state.get("experiment") or {}
            if (
                receipt.get("status") != "completed"
                or receipt.get("artifact") != plan["expected"]
                or not receipt.get("sha256")
            ):
                raise ValueError("Complete the planned experiment before rendering its measured results.")
            script = _text(args, "script") if "script" in args else None
            selection = {k: v for k, v in args.items() if k not in {"script", "filename"}}
            if not script and not selection:
                raise ValueError("Choose measured x and series fields, or supply a plotting script.")
            options = {"selection": selection or None, "measurement_sha256": receipt["sha256"]}
        before = self.state.get("plots", {}).get(filename) if plot else self.state.get("experiment")
        deadline = time.monotonic() + 20
        while True:
            record = native_experiment.execute(self.root, self.config, plan, script, plot=filename, **options)
            if (
                record["status"] in {"completed", "failed", "cancelled", "cancelling"}
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(2)
        if plot:
            self.state.setdefault("plots", {})[filename] = record
            if record["status"] == "completed" and record.get("input_access") == "worker_observed_read":
                manifest = figure_manifest.load_manifest(self.figures_dir, infer_existing=False)
                figure_manifest.register_figure(
                    manifest,
                    filename,
                    "matplotlib",
                    figures_dir=self.figures_dir,
                    input_sha256=record["input_sha256"],
                    input_access=record["input_access"],
                    script=script,
                    artifact_sha256=record["sha256"],
                    **({"plot_spec": record["plot_spec"]} if "plot_spec" in record else {}),
                )
                figure_manifest.save_manifest(self.figures_dir, manifest)
        else:
            self.state["experiment"] = record
        self.save()
        if record != before:
            self.event(
                "experiments",
                "plot" if plot else "measurement",
                f"{record['backend']} job {record['job_id']}: {record['status']}",
                status=record["status"],
                id=hashlib.sha256(
                    f"{record.get('identity', record['job_id'])}/{record['status']}".encode()
                ).hexdigest(),
                **({"artifact": record["artifact"]} if "artifact" in record else {}),
            )
        if not plot and record["status"] == "completed":
            data, _ = self._measurements()
            return {
                **record,
                "result_arrays": measurement_table.describe(data),
                "measurements_excerpt": (self.root / record["artifact"]).read_text()[:16000],
                "table_tool": "Use render_results_table to format measured columns in the paper without transcription.",
            }
        if plot and record["status"] == "completed" and not record.get("input_access"):
            return {
                **record,
                "measurement_note": "This earlier plot receipt has no measurement-read evidence. "
                "Use render_plot with measured columns, or explicitly render a custom plot under a new filename.",
            }
        return record

    def run_experiment(self, args):
        return self._execute(args)

    def _measurements(self):
        receipt = self.state.get("experiment") or {}
        name = receipt.get("artifact", "")
        if receipt.get("status") != "completed" or not re.fullmatch(r"results/[\w-]+\.json", name):
            raise ValueError("Complete the measured experiment first.")
        path = self.root / name
        if path.resolve() != path or _sha(path) != receipt.get("sha256"):
            raise ValueError("Measured artifact does not match its worker receipt.")
        return json.loads(path.read_text()), receipt["sha256"]

    def render_results_table(self, args):
        data, digest = self._measurements()
        available = measurement_table.describe(data)
        if not args.get("columns"):
            return {"available": available}
        name = _text(args, "name", limit=64)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
            raise ValueError("Choose a simple table basename.")
        tex = measurement_table.render(data, args.get("rows_pointer", ""), args["columns"])
        path = self.latex_dir / "tables" / (name + ".tex")
        if path.resolve() != path:
            raise ValueError("Unsafe table output path.")
        _atomic_write_text(path, tex)
        record = {
            "path": path.relative_to(self.latex_dir).as_posix(),
            "sha256": _sha(path),
            "input_sha256": digest,
            "columns": args["columns"],
            "rows_pointer": args.get("rows_pointer", ""),
        }
        self.state.setdefault("tables", {})[name] = record
        self.save()
        return {**record, "include": r"\input{" + record["path"] + "}", "preview": tex[:4000]}

    def render_plot(self, args):
        if not args:
            return {"available": measurement_table.describe(self._measurements()[0])}
        return self._execute(args, plot=True)

    def generate_concept_figure(self, args):
        from ark import nano_banana

        if not self.state.get("plan") or self.state.get("pending_plan_change"):
            raise ValueError(
                "Declare the approved experiment plan before generating its method illustration."
            )
        name = _text(args, "name", limit=64).removesuffix(".png")
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", name):
            raise ValueError("Choose a simple figure basename.")
        caption = _text(args, "caption", limit=1000)
        description = _text(args, "description", limit=9000)
        representation = args.get("representation", "study_panel")
        if representation not in ("study_panel", "schematic"):
            raise ValueError("Choose study_panel for comparison arms or schematic for a mechanism diagram.")
        spec = {"name": name, "caption": caption, "section_context": description, "placement": "full_width"}
        path = self.figures_dir / (name + ".png")
        manifest = figure_manifest.load_manifest(self.figures_dir, infer_existing=False)
        entry = manifest["figures"].get(path.name) or {}
        legacy_fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        spec["representation"] = representation
        design = nano_banana.native_study_design(self.state, self.state_dir)
        design_sha = hashlib.sha256(design.encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps([spec, design_sha], sort_keys=True).encode()).hexdigest()
        previous = _load(self.state_dir / "native_review.json", {})
        reviewed = previous.get("artifact_hashes", {}).get(str(path.relative_to(self.root)))
        current = self.state.get("concept") or {}
        actual_sha = _sha(path) if path.is_file() else None
        verified_image = (
            actual_sha and current.get("path") == str(path.relative_to(self.root))
            and actual_sha == current.get("sha256") == entry.get("artifact_sha256")
            and entry.get("source") == "nano_banana" and entry.get("provider") == "google"
            and current.get("semantic_pass")
        )
        existing_kind = "study_panel" if entry.get("representation") == "subject_with_comparison_panel" else "schematic"
        legacy = not entry.get("native_design_sha256") and (
            "representation" not in args or representation == existing_kind
        )
        same_request = entry.get("native_description") == fingerprint
        if verified_image:
            current = {**current, "representation": existing_kind}
            if existing_kind == "study_panel" and (entry.get("panel_plan") or {}).get("caption"):
                current["caption"] = entry["panel_plan"]["caption"]
        legacy_request = legacy and entry.get("native_description") == legacy_fingerprint
        unversioned = legacy and not entry.get("native_description") and "representation" not in args
        if verified_image and reviewed and reviewed != actual_sha and (same_request or legacy_request or unversioned):
            return {
                **current,
                "already_revised": True,
                "next": "This image changed after the recorded review. Compile and inspect the updated PDF.",
            }
        if verified_image and (same_request or legacy_request):
            return {**current, "cached": True}
        key = deep_research.get_gemini_api_key()
        if not key:
            raise RuntimeError("Nano Banana requires a Google key on the project owner's account.")
        if (
            entry.get("native_description") != fingerprint
            or not path.exists()
            or entry.get("artifact_sha256") != _sha(path)
        ):
            if self.state.get("image_intent") == fingerprint:
                raise RuntimeError(
                    "Image request outcome is ambiguous. Inspect the receipt before repeating it."
                )
            self.state["image_intent"] = fingerprint
            self.save()
            receipt = {}
            if representation == "study_panel":
                text_model = self.config.get("paperbanana_text_model") or nano_banana.TEXT_MODELS["flash"]
                receipt = nano_banana.create_study_panel(
                    path, design, "google", key, text_model.removeprefix("google/").removeprefix("gemini/"), "16:9"
                )
                if not receipt:
                    raise RuntimeError("No source-grounded study panel was produced. Check the plan and image receipt.")
            elif not nano_banana.generate_figure(
                "Create a method illustration. Show the experimental design. Omit numerical outcomes, fitted values, "
                "probability scores and performance trends. The data-derived plot reports measured results.\n"
                + design + "\nRequested presentation (use only supported facts):\n" + description,
                path,
                key,
                self.config.get("nano_banana_model") or "flash",
                aspect_ratio="16:9",
                receipt=receipt,
            ):
                raise RuntimeError("Google returned no usable image. Check provider status before retrying.")
            figure_manifest.register_figure(
                manifest,
                path.name,
                "nano_banana",
                figures_dir=self.figures_dir,
                placement="full_width",
                native_description=fingerprint,
                native_design_sha256=design_sha,
                **receipt,
            )
            figure_manifest.save_manifest(self.figures_dir, manifest)
        entry = manifest["figures"][path.name]
        if entry.get("representation") == "subject_with_comparison_panel":
            spec["caption"] = entry["panel_plan"]["caption"]
            spec["section_context"] = "Subject illustration with typeset comparison arms from the approved experiment."
        specs_file = self.figures_dir / "concept_figures.json"
        specs = [s for s in _load(specs_file, []) if s.get("name") != name] + [spec]
        _json(specs_file, specs)
        passed = self._check_final_concept_image(spec, path, manifest, key, "16:9")
        self.state["concept"] = self._concept_record(path, entry, passed)
        self.save()
        self.event(
            "figures",
            "concept_checked",
            "Method illustration verified." if passed else "Method illustration needs revision.",
            artifact=str(path.relative_to(self.root)),
        )
        return self.state["concept"]

    def _concept_record(self, path, entry, passed):
        panel = entry.get("representation") == "subject_with_comparison_panel"
        record = {
            "path": str(path.relative_to(self.root)), "sha256": _sha(path), "semantic_pass": passed,
            "representation": "study_panel" if panel else "schematic",
        }
        if passed and panel and (caption := (entry.get("panel_plan") or {}).get("caption")):
            record["caption"] = caption
        return record

    def lookup_papers(self, args):
        query = _text(args, "query", limit=500)
        limit = args.get("limit", 4)
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError("Choose between one and eight papers.")
        papers = citation.search_papers(query, max_results=limit)
        return {
            "papers": [
                {
                    "title": p.title,
                    "authors": p.authors,
                    "year": p.year,
                    "doi": p.doi,
                    "abstract": (p.abstract or "")[:2000],
                    "bibtex": citation.fetch_bibtex(p),
                }
                for p in papers[:limit]
            ]
        }

    def _source_record(self, entry):
        fields = entry["fields"]
        title = fields.get("title", "").replace("{", "").replace("}", "").strip()
        if not title:
            return None
        doi = citation._doi_id(fields.get("doi"))
        record = None
        if doi:
            data = citation._http_get_json("https://api.crossref.org/works/" + quote(doi, safe=""))
            meta = (data or {}).get("message") or {}
            returned_title = (meta.get("title") or [""])[0]
            if (
                str(meta.get("DOI", "")).lower() == doi
                and citation.title_similarity(title, returned_title) >= citation._SIMILARITY_THRESHOLD
            ):
                abstract = unescape(re.sub(r"<[^>]+>", " ", meta.get("abstract") or "")).strip()
                if abstract:
                    record = {
                        "source": "Publisher-deposited abstract (Crossref)",
                        "url": "https://doi.org/" + doi,
                        "title": returned_title,
                        "passage": abstract,
                    }
        if record is None:
            # The archive supplies the author-written abstract even when the publisher only deposited metadata.
            candidates = citation._search_arxiv(title, max_results=3, title_only=True)
            matching = citation._same_paper(title, fields, candidates)
            if (
                matching
                and matching.abstract
                and matching.arxiv_id
                and re.fullmatch(r"(?:[a-zA-Z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?", matching.arxiv_id)
            ):
                record = {
                    "source": "Author-deposited abstract (arXiv)",
                    "url": "https://arxiv.org/abs/" + matching.arxiv_id,
                    "title": matching.title,
                    "passage": matching.abstract,
                }
        if not record:
            return None
        record["bibtex_key"] = entry["key"]
        name = hashlib.sha256(record["url"].encode()).hexdigest()[:24] + ".txt"
        path = self.root / "sources" / name
        _atomic_write_text(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        return str(path.relative_to(self.root))

    def verify_citations(self, args):
        bib = self.latex_dir / "references.bib"
        digest = _sha(bib)
        saved = _load(self.state_dir / "native_citations.json", {})
        cited = citation.cited_keys_in_dir(self.latex_dir)
        sources = [r for r in saved.get("checked", {}).values() if r.get("source")]
        if (
            saved.get("sha256") == digest
            and saved.get("verified")
            and saved.get("cited") == sorted(cited)
            and saved.get("version") == 2
            and all(
                (self.root / r["source"]).is_file()
                and _sha(self.root / r["source"]) == r.get("source_sha256")
                for r in sources
            )
        ):
            return saved
        # Tags in author-editable BibTeX are not provenance for the verifier.
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / "references.bib"
            clean.write_text(
                "\n".join(line for line in bib.read_text().splitlines() if not line.lstrip().startswith("%"))
            )
            rows = citation.verify_bib(str(clean))
        checked = {r.entry_key: {"status": r.status, "detail": r.details} for r in rows}
        # Corrected bibliographic records are applied before stamping the digest.
        if any(r.status == "CORRECTED" for r in rows):
            citation.fix_bib(str(bib), [r for r in rows if r.status == "CORRECTED"])
        for entry in citation.parse_bib(str(bib)):
            key = entry["key"]
            if key not in cited or key not in checked:
                continue
            match = literature_evidence.resolve(entry)
            if not match and checked[key]["status"] == "NEEDS-CHECK":
                match = literature_evidence.crossref_record(entry["fields"])
            if match:
                if checked[key]["status"] == "NEEDS-CHECK":
                    checked[key].update(
                        status="SINGLE_SOURCE",
                        detail=(
                            f"Identity matched the indexed record {match['index']}. "
                            + (
                                "Publication year agrees."
                                if match.get("year")
                                else "Exact title and first two authors agree; index omits publication year."
                            )
                        ),
                    )
                if match.get("passage"):
                    filename = hashlib.sha256(match["url"].encode()).hexdigest()[:24] + ".txt"
                    path = self.root / "sources" / filename
                    _atomic_write_text(
                        path,
                        match["source"]
                        + "\n"
                        + match["title"]
                        + "\n"
                        + match["url"]
                        + "\n\n"
                        + match["passage"]
                        + "\n",
                    )
                    checked[key]["source"] = str(path.relative_to(self.root))
            if not checked[key].get("source") and checked[key]["status"] in {
                "VERIFIED",
                "CORRECTED",
                "SINGLE_SOURCE",
            }:
                path = self._source_record(entry)
                if path:
                    checked[key]["source"] = path
        missing = sorted(
            k
            for k in cited
            if checked.get(k, {}).get("status") not in {"VERIFIED", "CORRECTED", "SINGLE_SOURCE"}
        )
        for row in checked.values():
            if row.get("source"):
                row["source_sha256"] = _sha(self.root / row["source"])
        result = {
            "version": 2,
            "sha256": _sha(bib),
            "cited": sorted(cited),
            "checked": checked,
            "unconfirmed": missing,
            "verified": bool(cited) and not missing,
        }
        _json(self.state_dir / "native_citations.json", result)
        return result

    def _recheck_concept(self):
        # A changed plan can invalidate a previously approved diagram. Recheck the
        # stored image and spec using the same cache as generation and delivery.
        concept = self.state.get("concept")
        if not concept or self.config.get("figure_generation") != "nano_banana":
            return
        path = self.root / concept["path"]
        specs = _load(self.figures_dir / "concept_figures.json", [])
        spec = next((item for item in specs if item.get("name") == path.stem), None)
        manifest = figure_manifest.load_manifest(self.figures_dir, infer_existing=False)
        if not spec or not path.is_file() or path.name not in manifest["figures"]:
            return
        key = deep_research.get_gemini_api_key()
        passed = self._check_final_concept_image(spec, path, manifest, key, "16:9")
        self.state["concept"] = self._concept_record(path, manifest["figures"][path.name], passed)
        self.save()

    def _rendered_body_length(self):
        import fitz

        path = self.latex_dir / "main.pdf"
        if self.config.get("venue_format") == "icml":
            with fitz.open(path) as doc:
                boundary = body_page_boundary(doc)
                if boundary and boundary[0] > 0 and not any(boundary[1]):
                    page = doc[boundary[0] - 1]
                    width, height = page.rect.width, page.rect.height
                    boxes = [fitz.Rect(b[:4]) for b in self._body_text_blocks(page)]
                    boxes.extend(
                        fitz.Rect(b)
                        for kind, b in page.get_bboxlog()
                        if kind in {"fill-image", "fill-path", "stroke-path"}
                        and width * 0.06 < b[0] < b[2] < width * 0.94
                        and height * 0.06 < b[1] < b[3] < height * 0.95
                    )
                    column_height = 9 * 72  # ICML's nine-inch type area
                    top = (height - column_height) / 2
                    bottoms = [
                        max((b.y1 for b in boxes if b.x0 < right and b.x1 > left), default=top)
                        for left, right in ((width * 0.12, width / 2 - 8), (width / 2 + 8, width * 0.88))
                    ]
                    fill = sum(min(column_height, max(0, y - top)) for y in bottoms) / (2 * column_height)
                    return boundary[0] - 1 + fill
        return self._count_body_pages(path)

    def _link_appendix(self):
        if self.config.get("layout_mode") != "strict":
            return
        source = self.latex_dir / "main.tex"
        original = source.read_text()
        match = re.search(r"\\appendix\b", original)
        if not match or not re.search(r"\\section\{[^}]+\}", original[match.end() :]):
            return
        appendix = original[match.end() :]
        body = original[: match.start()]
        labels = re.findall(r"\\label\{([^}]+)\}", appendix)
        if any(re.search(r"\\(?:auto|page|name|c|C)?ref\{" + re.escape(key) + r"\}", body) for key in labels):
            return
        section = re.search(r"\\section\{[^}]+\}(?:\s*\\label\{([^}]+)\})?", appendix)
        label = section[1] or "app:i2p-supplement"
        if not section[1]:
            appendix = appendix[: section.end()] + r"\label{" + label + "}" + appendix[section.end() :]
        marker = body.find(r"\ifdefined\pdfdest\pdfdest name{idea2paper.body.end}")
        if marker < 0:
            return
        body = (
            body[:marker]
            + "Additional details appear in Appendix~"
            + r"\ref{"
            + label
            + "}.\n\n"
            + body[marker:]
        )
        _atomic_write_text(source, body + match[0] + appendix)

    def _apply_figure_layout(self):
        """Place a single wide registered figure at its intended scale in a two-column venue."""
        if self.config.get("layout_mode") != "strict" or self.config.get("venue_format") != "icml":
            return
        source = self.latex_dir / "main.tex"
        records = figure_manifest.load_manifest(self.figures_dir, infer_existing=False)["figures"]
        graphics = re.compile(r"\\includegraphics\s*\[(?P<options>[^\]]*)\]\s*\{(?P<file>[^{}]+)\}")
        width = re.compile(r"(\bwidth\s*=\s*)([0-9.]*)\\(textwidth|columnwidth|linewidth)")

        def resize(match):
            block = match.group()
            images = list(graphics.finditer(block))
            if len(images) != 1 or block.count(r"\includegraphics") != 1:
                return block
            image = images[0]
            if records.get(Path(image["file"]).name, {}).get("placement") != "full_width":
                return block
            if not width.search(image["options"]):
                return block
            updated = width.sub(lambda m: m[1] + r"\textwidth", image["options"])
            block = block[: image.start("options")] + updated + block[image.end("options") :]
            return block.replace(r"\begin{figure}", r"\begin{figure*}").replace(
                r"\end{figure}", r"\end{figure*}"
            )

        tex = source.read_text()
        updated = re.sub(r"\\begin\{figure(\*?)\}.*?\\end\{figure\1\}", resize, tex, flags=re.S)
        if updated != tex:
            _atomic_write_text(source, updated)

    def _fit_overfull_tables(self):
        """Use modest cell spacing for single-column tables that the preceding TeX build found too wide."""
        source = self.latex_dir / "main.tex"
        log = self.latex_dir / "main.log"
        if (
            self.config.get("layout_mode") != "strict"
            or not log.is_file()
            or not self.state.get("compiled_sources")
        ):
            return
        # LaTeX reports line numbers of the source it actually compiled.
        if self.state["compiled_sources"] != _paper_sources(self.latex_dir):
            return
        spans = [
            (int(a), int(b))
            for a, b in re.findall(
                r"Overfull \\hbox[^\n]*?at lines (\d+)--(\d+)", log.read_text(errors="replace")
            )
        ]
        if not spans:
            return
        tex = source.read_text()

        def correct(match):
            start = tex.count("\n", 0, match.start()) + 1
            end = start + match.group().count("\n")
            if (
                any(start <= b and a <= end for a, b in spans)
                and r"\tabcolsep" not in match.group()
                and r"\begin{tabular}" in match.group()
            ):
                return match.group().replace(
                    r"\begin{tabular}", r"\setlength{\tabcolsep}{4pt}" + "\n" + r"\begin{tabular}", 1
                )
            return match.group()

        updated = re.sub(r"\\begin\{table\}.*?\\end\{table\}", correct, tex, flags=re.S)
        if updated != tex:
            _atomic_write_text(source, updated)

    def _restore_measured_table(self):
        """Retain the requested worker table across prose rewrites."""
        tables = self.state.get("tables", {})
        if not tables or self.config.get("layout_mode") != "strict":
            return
        source = self.latex_dir / "main.tex"
        tex = source.read_text()
        used = {file.relative_to(self.latex_dir).as_posix() for file, _ in citation.active_tex_sources(self.latex_dir)}
        if any(row["path"] in used for row in tables.values()):
            return
        latest = next(reversed(tables.values()))
        path = self.latex_dir / latest["path"]
        measurement = self.state.get("experiment") or {}
        if (
            not path.is_file()
            or _sha(path) != latest["sha256"]
            or latest["input_sha256"] != measurement.get("sha256")
        ):
            return
        table = "\n".join(
            [
                "",
                r"\begin{table*}[t]",
                r"\centering",
                r"\small",
                r"\caption{Recorded experimental measurements.}\label{tab:i2p-measurements}",
                r"\input{" + latest["path"] + "}",
                r"\end{table*}",
                "",
            ]
        )
        end = tex.rfind(r"\end{document}")
        if end < 0:
            return
        body = re.split(r"\\(?:appendix|bibliography)\b", tex[:end], maxsplit=1)[0]
        results = re.search(
            r"\\(?:sub)*section\*?\{[^{}]*(?:results|findings|实验结果|测量结果)[^{}]*\}", body, re.I
        )
        if results:
            index = results.end()
            amended = tex[:index] + "\n" + table + tex[index:]
        else:
            intro = "" if re.search(r"\\appendix\b", tex) else r"\clearpage\appendix" + "\n"
            supplement = (
                intro + r"\section{Recorded measurements}\label{app:i2p-measured-results}" + "\n" + table
            )
            amended = tex[:end] + supplement + tex[end:]
        _atomic_write_text(source, amended)

    def _suggest_tex_edit(self, staging, log):
        """Offer a scoped typesetting edit for the misplaced underscore TeX reported."""
        location = re.search(r"(?m)^\./([^:\n]+\.tex):(\d+): Missing \$ inserted", log)
        if not location:
            return None
        file = (self.latex_dir / location[1]).resolve()
        if not file.is_relative_to(self.latex_dir) or not file.is_file():
            return None
        source = file.read_text()
        if source != (staging / location[1]).read_text():
            return None
        lines = source.splitlines()
        number = int(location[2])
        if not 1 <= number <= len(lines):
            return None
        fences = [i for i, line in enumerate(lines) if re.fullmatch(r"\s*```[\w-]*\s*", line)]
        before = [i for i in fences if i < number - 1]
        if len(before) % 2 and len(fences) > len(before):
            first, last = before[-1], fences[len(before)]
            if lines[last].strip() != "```" or last - first > 40:
                return None
            literal = "\n".join(lines[first:last + 1])
            amended = "\n".join([r"\begin{verbatim}", *lines[first + 1:last], r"\end{verbatim}"])
        else:
            code = re.search(r"\\texttt\{[a-zA-Z][a-zA-Z0-9.]*_[a-zA-Z0-9._]+\}", lines[number - 1])
            if not code:
                return None
            literal = code[0]
            amended = literal.replace("_", r"\_")
        return {"path": str(file.relative_to(self.root)), "replace": literal,
                "content": amended, "expected_matches": source.count(literal)}

    def check_manuscript_edit(self, args):
        """Allow incremental drafting, but catch edits that break a previously built paper."""
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("Expected the full proposed main.tex as UTF-8 text.")
        candidate = content.encode()
        if len(candidate) > 512 * 1024:
            raise ValueError("Expected the proposed main.tex as UTF-8 text up to 512 KiB.")
        source = self.latex_dir / "main.tex"
        if source.resolve() != source:
            raise ValueError("The manuscript must be a regular file inside paper/.")
        previous = source.read_bytes() if source.is_file() else None
        if "base_sha256" in args and (
            previous is None or hashlib.sha256(previous).hexdigest() != args["base_sha256"]
        ):
            return {"ok": False, "checked": False, "diagnostic": "The manuscript changed. Reopen the current revision."}
        pdf = self.latex_dir / "main.pdf"
        receipt = self.state.get("compile") or {}
        digest = self.state.get("compiled_pdf_sha256")
        if (
            previous is None or receipt.get("ok") is not True or not digest or receipt.get("pdf_sha256") != digest
            or pdf.is_symlink() or not pdf.is_file() or _sha(pdf) != digest
        ):
            return {"ok": True, "checked": False, "reason": "No verified prior build."}
        try:
            failure = _manuscript_first_pass(self.latex_dir, candidate)
            if failure is None:
                return {"ok": True, "checked": True}
            baseline_failure = _manuscript_first_pass(self.latex_dir, previous)
        except subprocess.TimeoutExpired:
            return {"ok": False, "checked": False, "diagnostic": "The manuscript check timed out after 90 seconds."}
        except OSError as exc:
            return {"ok": False, "checked": False, "diagnostic": f"The manuscript check could not run: {exc}"}
        if baseline_failure is not None:
            return {
                "ok": True, "checked": False,
                "reason": "The current manuscript also fails its first TeX pass. Incremental repair is allowed.",
                "diagnostic": failure,
                "baseline_diagnostic": baseline_failure,
            }
        return {"ok": False, "checked": True, "diagnostic": failure}

    def compile_paper(self, args):
        self._fit_overfull_tables()
        citation_proof = None
        if (self.latex_dir / "references.bib").is_file() and citation.cited_keys_in_dir(self.latex_dir):
            citation_proof = self.verify_citations({})
        delivery_contract.ensure_disclosure_in_tex(self.latex_dir / "main.tex")
        self._restore_measured_table()
        self._apply_figure_layout()
        if self.config.get("layout_mode") == "strict":
            ExecutionMixin.prepare_endmatter(self.latex_dir, self.log)
            self._link_appendix()
        self._recheck_concept()
        self._disclose_study_panel_art(self.latex_dir / "main.tex")
        source = self.latex_dir / "main.tex"
        digest = _paper_sources(self.latex_dir)
        if self.state.get("compiled_sources") == digest and (self.latex_dir / "main.pdf").is_file():
            if "body_equivalent_pages" in self.state.get("compile", {}) and self.state.get(
                "compiled_pdf_sha256"
            ) == _sha(self.latex_dir / "main.pdf"):
                return self.state["compile"]
        with tempfile.TemporaryDirectory(prefix="idea2paper-tex-") as tmp:
            staging = Path(tmp)
            env = _stage_tex(self.latex_dir, staging)
            if "TEXMFVAR" in os.environ:
                env["TEXMFVAR"] = os.environ["TEXMFVAR"]
            commands = (
                [_PDFLATEX, ["bibtex", "main"], _PDFLATEX, _PDFLATEX]
                if re.search(r"\\bibliography\s*\{", source.read_text())
                else [_PDFLATEX, _PDFLATEX]
            )
            for cmd in commands:
                edit = None
                try:
                    run = subprocess.run(cmd, cwd=staging, env=env, capture_output=True, timeout=90)
                except subprocess.TimeoutExpired:
                    error = f"{cmd[0]} timed out after 90 seconds."
                else:
                    if run.returncode == 0:
                        continue
                    error = (run.stdout + run.stderr).decode(errors="replace").strip()[-2000:]
                    if cmd[0] == "pdflatex" and (staging / "main.log").exists():
                        error = extract_latex_errors(staging / "main.log")
                        edit = self._suggest_tex_edit(staging, (staging / "main.log").read_text(errors="replace"))
                        if edit:
                            error += " Apply suggested_edit with write_project_file and compile the updated source."
                failure = {"ok": False, "source_sha256": digest, "diagnostic": error, "manuscript_issues": [error]}
                if edit:
                    failure["suggested_edit"] = edit
                changed = self.state.get("compile") != failure
                self.state["compile"] = failure
                self.state.pop("compiled_sources", None)
                self.state.pop("compiled_pdf_sha256", None)
                self.save()
                if changed:
                    self.event("writing", "paper_compile_failed", error[:700])
                return failure
            import fitz

            with fitz.open(staging / "main.pdf") as pdf:
                pages = len(pdf)
            local_inputs = set()
            for line in (staging / "main.fls").read_text(errors="replace").splitlines():
                if line.startswith("INPUT "):
                    path = (staging / line[6:]).resolve()
                    if path.is_relative_to(staging):
                        local_inputs.add(path.relative_to(staging).as_posix())
            cited = set()
            for aux in staging.rglob("*.aux"):
                for keys in re.findall(r"\\citation\{([^{}]+)\}", aux.read_text(errors="replace")):
                    cited.update(k.strip() for k in keys.split(","))
            if digest != _paper_sources(self.latex_dir) or digest != _paper_sources(staging):
                return {
                    "ok": False,
                    "diagnostic": "The manuscript changed while TeX was compiling. Compile its current revision.",
                }
            for suffix in ("aux", "bbl", "blg", "log", "pdf"):
                new = staging / ("main." + suffix)
                if new.exists():
                    dest = self.latex_dir / ("main." + suffix)
                    tmpfile = dest.with_name("." + dest.name + ".partial")
                    shutil.copyfile(new, tmpfile)
                    os.replace(tmpfile, dest)
        result = {
            "ok": True,
            "pages": pages,
            "inputs": sorted(local_inputs),
            "cited": sorted(cited),
            "overflow": parse_overfull_warnings(self.latex_dir),
            "pdf_sha256": _sha(self.latex_dir / "main.pdf"),
        }
        report = delivery_contract.evaluate(self.root, self.config.get("venue_pages", 4), config=self.config)
        result["checks"] = [
            dict(check=x.check, status=x.status, detail=x.detail)
            for x in report.findings
            if x.status != "pass"
        ]
        if self.config.get("layout_mode") == "strict":
            body = self._rendered_body_length()
            target = float(self.config.get("venue_pages") or 4)
            result["body_equivalent_pages"] = body
            if body and body < target - 0.25:
                result["layout_guidance"] = (
                    f"The body occupies about {body:.2f} of {target:g} requested pages. "
                    "Use the remaining space to report the actual paired results, diagnose uncertainty and "
                    "explain where the evidence stops. Preserve the venue margins and type size."
                )
        result["editorial_notes"] = [f.detail for f in report.violations if f.check in {"body_prose_repeated", "markdown_emphasis"}]
        if citation_proof:
            result["unverified_citations"] = citation_proof["unconfirmed"]
        self.state.update(compiled_sources=digest, compile=result, compiled_pdf_sha256=result["pdf_sha256"])
        result["manuscript_issues"] = self._strict_paper_issues()
        self.save()
        if result["overflow"] and not getattr(self, "_layout_retry", False):
            self._fit_overfull_tables()
            if digest != _paper_sources(self.latex_dir):
                self._layout_retry = True
                try:
                    return self.compile_paper(args)
                finally:
                    self._layout_retry = False
        self.event("writing", "paper_compiled", f"Rendered {pages} pages.", artifact="paper/main.pdf")
        return result

    def _snapshot(self):
        required = ["manuscript", "experiment"]
        if self.config.get("layout_mode") == "strict":
            required.extend(("citations", "visual"))
        return review.build_snapshot(
            self.root,
            target_track=str(self.config.get("venue") or self.config.get("venue_format") or ""),
            required=required,
        )

    def _implementation_claims(self):
        """Flag explicit claims about a named API against the exact executed source."""
        experiment = self.state.get("experiment") or {}
        plan = self.state.get("plan") or {}
        script = self.root / plan.get("script", "")
        if (
            experiment.get("status") != "completed"
            or not script.is_file()
            or _sha(script) != experiment.get("code_sha256")
        ):
            return []
        try:
            tree = ast.parse(script.read_text())
        except (SyntaxError, UnicodeError):
            return []
        imports = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(
                    {
                        n.asname or n.name.split(".")[0]: n.name if n.asname else n.name.split(".")[0]
                        for n in node.names
                    }
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.update({n.asname or n.name: node.module + "." + n.name for n in node.names})
        calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                head, dot, rest = name.partition(".")
                calls.add(imports.get(head, head) + dot + rest)
        paper = "\n".join(source for _, source in citation.active_tex_sources(self.latex_dir)).replace(r"\_", "_")
        claimed = re.findall(
            r"(?:\busing(?: the)?|\bwe (?:used?|call(?:ed)?))\s+"
            r"\\texttt\{((?:scipy|sklearn|numpy|torch)\.[A-Za-z_][A-Za-z_0-9.]*)\}",
            paper,
            re.I,
        )
        return [
            f"The paper names {symbol} as an implementation step, but the executed {plan['script']} "
            "does not call it. Check the sealed method and correct the implementation description."
            for symbol in dict.fromkeys(claimed)
            if symbol not in calls
        ]

    def _strict_paper_issues(self):
        if self.config.get("layout_mode") != "strict":
            return []
        rendered = self.state.get("compile", {})
        issues = self._implementation_claims()
        main = self.latex_dir / "main.tex"
        if main.is_file() and main.resolve() == main:
            issues.extend(review.reported_change_conflicts(main.read_text()))
        if guidance := rendered.get("layout_guidance"):
            issues.append(guidance)
        if overflow := rendered.get("overflow"):
            issues.append("Repair the typesetting overflow identified by TeX: " + " | ".join(overflow[:4]))
        measurement = self.state.get("experiment") or {}
        if measurement.get("status") != "completed":
            return issues
        plots = self.state.get("plots", {})
        inputs = set(rendered.get("inputs", []))
        manifest = figure_manifest.load_manifest(self.figures_dir, infer_existing=False)
        names = set(plots) | {
            name for name, entry in manifest["figures"].items()
            if isinstance(entry, dict) and entry.get("source") == "matplotlib"
        }
        included = sorted(name for name in names if "figures/" + name in inputs)
        if not included:
            issues.append("Include a scientific plot generated from the current worker measurements in the rendered PDF.")
        for name in included:
            row = plots.get(name, {})
            path = self.figures_dir / name
            if (
                row.get("status") != "completed"
                or row.get("input_access") != "worker_observed_read"
                or row.get("input_sha256") != measurement.get("sha256")
                or row.get("artifact") != "paper/figures/" + name
                or path.resolve() != path or not path.is_file() or _sha(path) != row.get("sha256")
            ):
                issues.append(f"Render {name} from the current worker measurements with render_plot. "
                              "Its receipt must confirm that the worker read /study/results.json.")
        tables = {row["path"]: row for row in self.state.get("tables", {}).values()}
        hand_measured = False
        used = []
        for name in rendered.get("inputs", []):
            path = self.latex_dir / name
            if path.suffix != ".tex" or not path.is_file() or path.resolve() != path:
                continue
            if name in tables:
                used.append(name)
                if _sha(path) != tables[name]["sha256"] or tables[name]["input_sha256"] != measurement.get(
                    "sha256"
                ):
                    issues.append(f"Regenerate {name} from the current worker results.")
                continue
            for block in re.findall(
                r"\\begin\{tabular\*?\}(.*?)\\end\{tabular\*?\}", path.read_text(), flags=re.S
            ):
                # Consecutive indexed observations are particularly vulnerable to a
                # transcription error when only the beginning of a result fits a chat turn.
                if len(re.findall(r"(?m)^\s*\d+\s*&[^\n]*\d[^\n]*&[^\n]*\d", block)) >= 2:
                    hand_measured = True
        if hand_measured and not used and measurement_table.describe(self._measurements()[0]):
            issues.append(
                "A numbered results table has manually entered measurements. Use render_results_table "
                "to select the matching worker rows and columns, then replace the tabular block with the returned input."
            )
        return issues

    def _opening_review_evidence(self, snapshot):
        """Put measured inputs, plotting code and cited originals in the blind reader's first turn."""
        assets = snapshot["artifacts"]
        def select(names, kind, limit):
            return list(dict.fromkeys(n for n in names if assets.get(n, {}).get("kind") == kind))[:limit]

        measured = (self.state.get("experiment") or {}).get("artifact")
        measurements = select([measured, *sorted(assets)], "measurement", 1)
        registered = [item.get("script") for item in (self.state.get("plots") or {}).values()]
        # Imported manuscripts may have a plot script and no native run receipt.
        candidates = [name for name in sorted(assets) if re.search(r"plot|figure", Path(name).stem, re.I)]
        methods = select([*registered, *candidates], "method", 1)
        citations = _load(self.state_dir / "native_citations.json", {}).get("checked", {})
        originals = select([record.get("source") for record in citations.values()] + sorted(assets),
                           "primary_source", 2)
        # PDFs need a page selector. The reviewer can request them through read_evidence.
        return [path for path in [*methods, *measurements, *originals] if Path(path).suffix.lower() != ".pdf"]

    def review_snapshot(self, args):
        self._check_compiled()
        if args.get("preflight") and self.config.get("layout_mode") == "strict":
            audit = delivery_contract.evaluate(
                self.root, self.config.get("venue_pages", 4), config=self.config
            )
            blockers = [
                f"{finding.check}: {finding.detail}" for finding in audit.delivery_blockers(self.config)
            ]
            citations = _load(self.state_dir / "native_citations.json", {})
            bib = self.latex_dir / "references.bib"
            if not bib.is_file() or not citations.get("verified") or citations.get("sha256") != _sha(bib):
                blockers.append(
                    "Verify original sources for the current bibliography: "
                    + ", ".join(citations.get("unconfirmed", []))
                )
            blockers.extend(self._strict_paper_issues())
            if blockers:
                return {"blockers": blockers}
        self.snapshot = self._snapshot()
        snapshot = self.snapshot
        previous = _load(self.state_dir / "native_review.json")
        self.review_receipts = []
        archive = self.state_dir / "reviews" / snapshot["revision"]
        archive.mkdir(parents=True, exist_ok=True)
        saved = archive / "main.pdf"
        if not saved.exists():
            tmp = archive / ".main.pdf.tmp"
            shutil.copyfile(self.latex_dir / "main.pdf", tmp)
            os.replace(tmp, saved)
        if _sha(saved) != self.state["compiled_pdf_sha256"]:
            raise ValueError("Archived review PDF does not match the compiled manuscript.")
        for name in ("main.tex", "references.bib"):
            source = self.latex_dir / name
            if source.is_file() and not (archive / name).exists():
                _atomic_write_text(archive / name, source.read_text())
        record = _load(archive / "review.json")
        pages = 0
        if "visual" in snapshot.get("required", ()):
            import fitz

            with fitz.open(saved) as pdf:
                pages = len(pdf)
        result = {
            "revision": snapshot["revision"],
            "brief": review.review_prompt(snapshot, previous=previous),
            "rendered_artifact": f"{snapshot.get('paper_dir', 'paper')}/main.pdf",
            "rendered_pages": pages,
            "opening_evidence": self._opening_review_evidence(snapshot),
            "evidence_refs": sorted(snapshot["artifacts"]),
        }
        if record and review.record_is_current(snapshot, record):
            if previous != record:
                _json(self.state_dir / "native_review.json", record)
            result["cached"] = record
        elif args.get("session_id"):
            session = _text(args, "session_id", limit=160)
            attempts = self.state.setdefault("review_attempts", {})
            if session not in attempts:
                limit = int(self.config.get("max_review_attempts", 8))
                if len(attempts) >= limit:
                    raise ValueError(
                        f"Project review budget exhausted ({limit} independent reviews). "
                        "Inspect the outstanding findings before increasing max_review_attempts."
                    )
                attempts[session] = snapshot["revision"]
                self.save()
            elif attempts[session] != snapshot["revision"]:
                raise ValueError("Reviewer session belongs to a different manuscript revision.")
        return result

    def review_evidence(self, args):
        if not self.snapshot or args.get("revision") != self.snapshot["revision"]:
            raise ValueError("Open the current review snapshot before requesting evidence.")
        artifact = args.get("artifact") or args.get("ref")
        result = review.read_evidence(
            self.snapshot,
            artifact,
            page=args.get("page"),
            start=args.get("start", 1),
            visual=args.get("visual", False),
            statistic=args.get("statistic"),
            column=args.get("column", ""),
            rows_pointer=args.get("rows_pointer", ""),
        )
        self.review_receipts.append(result["receipt"])
        return result

    def record_review(self, args):
        snapshot = self._snapshot()
        if (
            not self.snapshot
            or snapshot["revision"] != args.get("revision")
            or snapshot["revision"] != self.snapshot["revision"]
        ):
            raise ValueError("Manuscript or evidence changed while the review was in progress.")
        observed = args.get("observed", [])
        if not isinstance(observed, list) or any(r not in self.review_receipts for r in observed):
            raise ValueError("Unknown reviewer evidence receipt.")
        previous = _load(self.state_dir / "native_review.json")
        record = review.validate_review(snapshot, args["verdict"], previous=previous, observed=observed)
        record.update(
            session_id=_text(args, "session_id", limit=160),
            paper_pdf_sha256=_sha(self.latex_dir / "main.pdf"),
        )
        if previous and previous["revision"] != record["revision"]:
            record["previous_revision"] = previous["revision"]
        archive = self.state_dir / "reviews" / snapshot["revision"]
        _json(archive / "review.json", record)
        _json(self.state_dir / "native_review.json", record)
        self.event(
            "review",
            "manuscript_reviewed",
            record["verdict"],
            revision=snapshot["revision"],
            artifact=str((archive / "main.pdf").relative_to(self.root)),
        )
        return record

    def _check_compiled(self):
        if (
            not (self.latex_dir / "main.pdf").is_file()
            or not self.state.get("compiled_pdf_sha256")
            or self.state.get("compiled_sources") != _paper_sources(self.latex_dir)
            or self.state["compiled_pdf_sha256"] != _sha(self.latex_dir / "main.pdf")
        ):
            raise ValueError(
                "Compile the current manuscript and figures before requesting review or delivery."
            )

    def deliver(self, args):
        self._check_compiled()
        snap = self._snapshot()
        record = _load(self.state_dir / "native_review.json", {})
        problems = []
        if not review.record_is_current(snap, record) or not record.get("deliverable"):
            problems.append("Independent review is missing, stale, or requires revision.")
        measurement = self.state.get("experiment") or {}
        measured = self.root / measurement.get("artifact", "")
        if (
            measurement.get("status") != "completed"
            or not measured.is_file()
            or measured.is_symlink()
            or _sha(measured) != measurement.get("sha256")
        ):
            problems.append("Missing or changed worker measurements.")
        if self.config.get("layout_mode") == "strict":
            rendered = self.state.get("compile", {})
            compiled_files = set(rendered.get("inputs", []))
            tex = "\n".join(
                (self.latex_dir / path).read_text()
                for path in [
                    "main.tex",
                    *sorted(p for p in compiled_files if p.endswith(".tex") and p != "main.tex"),
                ]
            )
            tex = re.sub(r"(?<!\\)%[^\n]*", "", tex)
            proof = _load(self.state_dir / "native_citations.json", {})
            bib = self.latex_dir / "references.bib"
            cited = set(rendered.get("cited", []))
            if (
                not bib.is_file()
                or not proof.get("verified")
                or proof.get("sha256") != _sha(bib)
                or not cited
                or not cited.issubset(set(proof.get("cited", [])))
            ):
                problems.append("Verify the sources cited by this manuscript.")
            parts = re.split(r"\\appendix\b", tex, maxsplit=1)
            if len(parts) < 2 or not re.search(r"\\(?:sub)*section\*?\{[^}]+\}", parts[1]):
                problems.append("Include a reproducibility appendix with a substantive section.")
        report = delivery_contract.evaluate(self.root, self.config.get("venue_pages", 4), config=self.config)
        delivery_contract.write_report(self.root, report)
        problems += [f"{x.check}: {x.detail}" for x in report.delivery_blockers(self.config)]
        problems.extend(self._strict_paper_issues())
        if problems:
            return {"delivered": False, "blockers": problems}
        self.state.update(state="delivered", delivery_revision=snap["revision"])
        self.save()
        self.event(
            "delivery",
            "delivered",
            "Verified paper and current independent review.",
            artifact="paper/main.pdf",
            revision=snap["revision"],
        )
        return {
            "delivered": True,
            "revision": snap["revision"],
            "pdf": "paper/main.pdf",
            "audit": "auto_research/state/delivery_report.yaml",
        }

    def invoke(self, op, args):
        allowed = {
            "inspect_project",
            "stop",
            "continue_project",
            "project_status",
            "ack_command",
            "project_event",
            "plan_experiment",
            "research",
            "run_experiment",
            "render_plot",
            "render_results_table",
            "generate_concept_figure",
            "lookup_papers",
            "verify_citations",
            "check_manuscript_edit",
            "compile_paper",
            "review_snapshot",
            "review_evidence",
            "record_review",
            "deliver",
        }
        if op not in allowed or not isinstance(args, dict):
            raise ValueError("Unknown project operation or invalid arguments.")
        if not self.state and op not in {"project_status", "inspect_project", "check_manuscript_edit"}:
            raise ValueError("Open the project before starting research.")
        if self.state.get("state") in {"paused", "stopped"} and op not in {
            "project_status",
            "inspect_project",
            "check_manuscript_edit",
            "project_event",
            "ack_command",
            "stop",
            "continue_project",
        }:
            raise ValueError("Project is paused. Resolve the pending gate before continuing.")
        return getattr(self, op)(args)


def serve(project, reader, writer):
    for line in reader:
        request = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Invalid request envelope.")
            with contextlib.redirect_stdout(sys.stderr):
                result = project.invoke(request.get("op"), request.get("args", {}))
            response = {"id": request.get("id"), "ok": True, "result": result}
        except Exception as error:
            response = {
                "id": request.get("id") if isinstance(request, dict) else None,
                "ok": False,
                "error": str(error)[:3000],
            }
        writer.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
        writer.flush()


if __name__ == "__main__":
    serve(NativeProject(Path.cwd()), sys.stdin, sys.stdout)
