"""Scientific operations exposed to the native Idea2Paper DSH bundle.

The native agent chooses which operation to call. These operations bind external
side effects and delivery decisions to the project's durable scientific record.
"""

from __future__ import annotations

import ast
import contextlib
import fcntl
from datetime import datetime, timezone
import hashlib
import json
import math
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
from ark.compute import native_experiment, review_control
from ark.execution import ExecutionMixin
from ark.latex.compiler import CompilerMixin, body_repetition_notes, body_sections_and_figures, rendered_body_images
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


def _manuscript_first_pass(paper, content, *, check_headings=False, page_target=None):
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
                return {"diagnostic": extract_latex_errors(log)[:2000]}
            return {"diagnostic": (run.stdout + run.stderr).decode(errors="replace").strip()[-2000:] or "pdflatex failed."}
        pdf = staging / "main.pdf"
        if not pdf.is_file() or not pdf.stat().st_size:
            return {"diagnostic": "pdflatex produced no manuscript pages."}
        if check_headings and (notes := body_repetition_notes(staging, empty_headings_only=True)):
            return {"diagnostic": notes[0]}
        if page_target:
            import fitz

            with fitz.open(pdf) as doc:
                boundary = body_page_boundary(doc)
            if boundary is not None:
                end_page, body_bottoms = boundary
                count = end_page + int(any(body_bottoms))
                preview = {"body_pages": count, "target": page_target}
                if count == page_target:
                    preview["next"] = ("The first TeX pass reaches the requested body length. Run compile_paper "
                                       "now to verify the final pagination, references and figures before further edits.")
                return {"first_pass_preview": preview}
        return {}


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
        for filename, old in list(self.state.get("plots", {}).items()):
            if old.get("status") != "completed":
                # Earlier workers overwrote the last published figure with its pending replacement.
                self.state.setdefault("plot_attempts", {})[filename] = old
                published = native_experiment.published_plot(self.root, filename)
                if published:
                    self.state["plots"][filename] = published
                else:
                    del self.state["plots"][filename]
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
        elif action not in {"turn/end", "turn_end", "tool/call", "tool/result", "failure", "quota", "stopped"}:
            artifact = f" ({row['artifact']})" if row.get("artifact") else ""
            self.log(f"[{phase}] {row['observation']}{artifact}")
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
                if not review.delivered_record_matches(current, verdict, self.state.get("delivery_revision"),
                                                       self.state.get("delivery_inputs_sha256")):
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
                "State which recorded observation supports each central inference. When revising a disputed claim, trace it across the "
                "abstract, discussion, conclusions and captions so that earlier versions of the explanation are corrected everywhere. "
                "Use the survey's official titles and primary abstracts to select relevant originals before writing. "
                "Register those works with lookup_papers and cite the returned keys at the specific comparisons. "
                "Use render_results_table to include the exact worker values in per-condition tables. "
                "Show the paired measurements with their conditions, units and denominators so a reader can reproduce the central comparison. "
                "Use the venue's body-page budget for evidence, uncertainty, interpretation and limitations; keep references and a substantive linked appendix after the body. "
                "Compile, verify citations, request an independent review, address consequential findings, then deliver. "
                "Call project tools when needed. Prefer reanalysis to a duplicate experiment. A negative result is valid. "
                "Report missing evidence rather than claiming success. Scientific scripts write their measurements as JSON to the registered results path or stdout. "
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
            "previous_measurement",
            "measurement_comparison",
            "research",
            "concept",
            "compiled_pdf_sha256",
            "compile",
            "delivery_revision",
            "plots",
            "plot_attempts",
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
        leads = self._survey_leads(literature_evidence.source_leads(report))
        return {
            "path": "auto_research/state/deep_research.md",
            "source_leads": leads,
            "source_guidance": "official_title and abstract_excerpt come from the DOI deposit. Survey titles and claims may be wrong. "
                               "Before writing related work, select relevant "
                               "ones with lookup_papers(query, add_to_bibliography=true). Check their official titles "
                               "and original passages before citing their returned keys. Keep the venue bibliography.",
            "report": report[:6000],
            "total_chars": len(report),
            "next_offset": 6000 if len(report) > 6000 else None,
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
        if "mechanism" in args:
            mechanism = args["mechanism"]
            fields = ("explanation", "alternative", "distinguishing_observation", "claim_without_it")
            if not isinstance(mechanism, dict) or set(mechanism) != set(fields):
                raise ValueError("For a mechanism study, describe the explanation, alternative, distinguishing_observation and claim_without_it.")
            plan["mechanism"] = {key: _text(mechanism, key, limit=1200) for key in fields}
            if plan["mechanism"]["explanation"].casefold() == plan["mechanism"]["alternative"].casefold():
                raise ValueError("Describe a distinct competing explanation.")
        if not re.fullmatch(r"scripts/[\w-]+\.py", plan["script"], re.ASCII):
            raise ValueError('script must be one Python file path such as "scripts/experiment.py".')
        if not re.fullmatch(r"results/[\w-]+\.json", plan["expected"], re.ASCII):
            raise ValueError('result_path must be one output file path such as "results/measurements.json".')
        if plan != self.state.get("plan"):
            filenames = {plan["script"], plan["expected"]}
            missing = [field for field in ("baseline", "comparison", "metric") if plan[field] in filenames]
            if all(control.strip() in filenames for control in controls):
                missing.append("controls")
            if missing:
                raise ValueError("Describe the scientific conditions in " + ", ".join(missing)
                                 + ". Give the two configurations being compared, the measured quantity, and what "
                                   "is held fixed. Use script and result_path for the implementation and output filenames.")
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
            raise ValueError("For a revised experiment, call plan_experiment with a new result_path, then "
                             "run_experiment. Choose a distinct result filename to preserve prior measurements.")
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
        if (existing and existing.get("status") == "completed" and existing.get("sha256")
                and existing.get("artifact") == (self.state.get("plan") or {}).get("expected")):
            self.state["previous_measurement"] = {k: existing[k] for k in ("artifact", "sha256")}
        self.state.pop("measurement_comparison", None)
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
        if previous := self.state.get("previous_measurement"):
            return {**plan, "previous_result": previous["artifact"], "revision_guidance":
                    "For a scientific correction, first run a small case with independently known behavior through "
                    "the same calculation used on the data. Record what distinguishes the old and intended methods."}
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
                if (self._measurement_receipt() or {}).get("status") == "completed":
                    raise ValueError("The previous experiment completed with its original code. To test this "
                                     "correction, call plan_experiment with a distinct result_path, then "
                                     "run_experiment. Regenerate any table or plot that uses the revised rows.")
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
            prior = self.state.get("plot_attempts", {}).get(filename) or {}
            options = {"selection": selection or None, "measurement_sha256": receipt["sha256"],
                       "prior_plot_identity": prior.get("identity")}
        before = self.state.get("plot_attempts", {}).get(filename) if plot else self.state.get("experiment")
        deadline = time.monotonic() + 20
        while True:
            record = native_experiment.execute(self.root, self.config, plan, script, plot=filename, **options)
            if (
                record["status"] in {"completed", "failed", "cancelled", "cancelling", "deferred", "submission_unknown"}
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(2)
        if plot:
            attempt = record.get("prior_attempt", record)
            self.state.setdefault("plot_attempts", {})[filename] = attempt
            if attempt["status"] == "completed":
                self.state.setdefault("plots", {})[filename] = attempt
            if attempt["status"] == "completed" and attempt.get("input_access") == "worker_observed_read":
                manifest = figure_manifest.load_manifest(self.figures_dir, infer_existing=False)
                figure_manifest.register_figure(
                    manifest,
                    filename,
                    "matplotlib",
                    figures_dir=self.figures_dir,
                    input_sha256=attempt["input_sha256"],
                    input_access=attempt["input_access"],
                    script=attempt.get("script"),
                    artifact_sha256=attempt["sha256"],
                    **({"plot_spec": attempt["plot_spec"]} if "plot_spec" in attempt else {}),
                )
                figure_manifest.save_manifest(self.figures_dir, manifest)
        else:
            self.state["experiment"] = record
            attempt = record
            if record["status"] == "completed" and self.state.get("previous_measurement"):
                self._compare_revised_measurement(record)
        self.save()
        if attempt != before:
            tracking = attempt.get("job_id") or attempt["identity"]
            observation = f"{attempt['backend']} {'job' if attempt.get('job_id') else 'submission'} {tracking}: {attempt['status']}"
            comparison = self.state.get("measurement_comparison", {})
            if not plot and attempt["status"] == "completed" and comparison.get("unchanged_arrays"):
                count = sum(group["rows"] for group in comparison["unchanged_arrays"])
                observation += (f". {count} result rows match {comparison['previous']} "
                                "at relative tolerance 1e-12. Check what distinguishes the revised calculation.")
            self.event(
                "experiments",
                "plot" if plot else "measurement",
                observation,
                status=attempt["status"],
                id=hashlib.sha256(
                    f"{attempt.get('identity') or tracking}/{attempt['status']}".encode()
                ).hexdigest(),
                **({"artifact": attempt["artifact"]} if "artifact" in attempt else {}),
            )
        if not plot and record["status"] == "completed":
            data, _ = self._measurements()
            return {
                **record,
                "result_arrays": measurement_table.describe(data),
                "measurements_excerpt": (self.root / record["artifact"]).read_text()[:16000],
                "table_tool": "Use render_results_table to format measured columns in the paper without transcription.",
                **({"revision_check": self.state["measurement_comparison"]} if self.state.get("measurement_comparison") else {}),
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

    def _compare_revised_measurement(self, record):
        prior = self.state["previous_measurement"]
        name = prior.get("artifact", "")
        if not re.fullmatch(r"results/[\w-]+\.json", name) or name == record["artifact"]:
            return
        path = self.root / name
        try:
            previous = native_experiment._read(path) if path.resolve() == path else None
        except (OSError, ValueError):
            previous = None
        if previous is None or hashlib.sha256(previous).hexdigest() != prior.get("sha256"):
            self.state.pop("measurement_comparison", None)
            return
        if (self.state.get("measurement_comparison") or {}).get("current_sha256") == record["sha256"]:
            return
        current, digest = self._measurements()
        same = measurement_table.unchanged_result_arrays(json.loads(previous), current)
        check = {"previous": name, "previous_sha256": prior["sha256"], "current": record["artifact"],
                 "current_sha256": digest, "unchanged_arrays": same, "float_relative_tolerance": 1e-12}
        if same:
            check["observation"] = ("The listed arrays match the earlier rows at a relative tolerance of 1e-12. "
                                    "If this change was meant to correct a measurement, test an input on which the old "
                                    "and proposed calculations predict different values before calling it fixed.")
        self.state["measurement_comparison"] = check

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
        pointer = args.get("rows_pointer", "")
        columns = args["columns"]
        if self.config.get("layout_mode") == "strict":
            columns = measurement_table.with_row_identifiers(data, pointer, columns)
            if isinstance(columns, list) and len(columns) > 12:
                raise ValueError("Keep each table within twelve columns including its trial or condition identifiers.")
        tex = measurement_table.render(data, pointer, columns)
        path = self.latex_dir / "tables" / (name + ".tex")
        if path.resolve() != path:
            raise ValueError("Unsafe table output path.")
        _atomic_write_text(path, tex)
        record = {
            "path": path.relative_to(self.latex_dir).as_posix(),
            "sha256": _sha(path),
            "input_sha256": digest,
            "columns": columns,
            "rows_pointer": pointer,
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
        purpose = args.get("purpose")
        if purpose is not None and purpose not in ("compare_conditions", "explain_mechanism"):
            raise ValueError("Choose compare_conditions or explain_mechanism for the figure purpose.")
        representation = args.get("representation", "study_panel")
        if representation not in ("study_panel", "schematic"):
            raise ValueError("Choose study_panel or schematic for the legacy representation.")
        if purpose is not None:
            representation = "schematic" if purpose == "explain_mechanism" else "study_panel"
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
        explicit_kind = purpose is not None or "representation" in args
        legacy = not entry.get("native_design_sha256") and (not explicit_kind or representation == existing_kind)
        same_request = entry.get("native_description") == fingerprint
        old_design_sha = entry.get("native_design_sha256")
        if not same_request and old_design_sha and old_design_sha != design_sha:
            prior_request = hashlib.sha256(json.dumps([spec, old_design_sha], sort_keys=True).encode()).hexdigest()
            same_request = (prior_request == entry.get("native_description") and
                            nano_banana.legacy_native_design_digest(self.state, self.state_dir) == old_design_sha)
        if verified_image:
            current = {**current, "representation": existing_kind}
            if existing_kind == "study_panel" and (entry.get("panel_plan") or {}).get("caption"):
                current["caption"] = entry["panel_plan"]["caption"]
        legacy_request = legacy and entry.get("native_description") == legacy_fingerprint
        unversioned = legacy and not entry.get("native_description") and not explicit_kind
        prior = entry.get("semantic_review") or {}
        if (actual_sha and actual_sha == prior.get("artifact_sha256") and (same_request or legacy_request or unversioned)
                and (conflict := nano_banana.generated_result_conflict(prior.get("checks")))):
            self.state["concept"] = self._concept_record(path, entry, False)
            self.save()
            return {**self.state["concept"], "cached": True, "diagnostic": conflict,
                    "next": "Request a method illustration limited to the protocol, and draw results with render_plot."}
        if prior and prior.get("version") != nano_banana.SEMANTIC_REVIEW_VERSION:
            verified_image = False
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
            not (same_request or legacy_request)
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
                    path, design, "google", key, text_model.removeprefix("google/").removeprefix("gemini/"), "16:9",
                    approved_plan=self.state["plan"],
                )
                if not receipt:
                    raise RuntimeError("No source-grounded study panel was produced. Check the plan and image receipt.")
            elif not nano_banana.generate_figure(
                "Illustrate the method in the approved protocol below. Depict the configuration, scientific objects, "
                "and operations that explain the comparison. Use schematic examples for geometry when appropriate. "
                "Omit performance panels, empirical results axes, trend lines and recorded observations.\n" + design
                + ("\nRequested mechanism to explain, subject to the approved protocol:\n" + description
                   if purpose == "explain_mechanism" else ""),
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
        if not passed:
            from ark.nano_banana import generated_result_conflict
            checks = (entry.get("semantic_review") or {}).get("checks") or {}
            if conflict := generated_result_conflict(checks):
                record["diagnostic"] = conflict + " Keep the results plot separate and render it from worker output."
            corrections = [{"check": name, "observed": check["observed"][:450], "correction": check["correction"][:450]}
                           for name, check in checks.items() if isinstance(check, dict) and check.get("status") == "contradiction"
                           and isinstance(check.get("observed"), str) and isinstance(check.get("correction"), str)]
            if corrections:
                record["corrections"] = corrections
        return record

    def _survey_leads(self, leads):
        originals = dict(self.state.get("survey_originals") or {})
        resolved = literature_evidence.survey_originals(leads, originals)
        if originals != self.state.get("survey_originals", {}):
            self.state["survey_originals"] = originals
            self.save()
        return resolved

    def lookup_papers(self, args):
        query = _text(args, "query", limit=500)
        limit = args.get("limit", 4)
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError("Choose between one and eight papers.")
        doi = citation._doi_id(query)
        wanted = citation._normalize_title(query)
        if (not doi and args.get("add_to_bibliography")
                and deep_research.research_report_status(self.state_dir) == "verified"):
            # Older checkpoints may have the survey but no resolved originals.
            leads = literature_evidence.source_leads((self.state_dir / "deep_research.md").read_text())
            originals = self.state.get("survey_originals") or {}
            candidates = [lead for lead in leads if wanted in {
                citation._normalize_title(lead.get("reported_title", "")),
                citation._normalize_title(originals.get(citation._doi_id(lead["query"]), {}).get("title", "")),
            }]
            exact = [citation._doi_id(lead["query"]) for lead in self._survey_leads(candidates)
                     if citation._normalize_title(lead.get("official_title", "")) == wanted]
            if len(exact) == 1:
                doi = exact[0]
        direct = None
        if doi and args.get("add_to_bibliography"):
            # A resolver returns the selected work, even when a title search ranks
            # similarly named papers higher. Verify the returned DOI before use.
            bib = citation._fetch_bibtex_from_doi(doi)
            entries = citation.parse_bib_string(citation._strip_bibtex_fields(bib)) if bib else []
            if len(entries) == 1 and citation._doi_id(entries[0]["fields"].get("doi")) == doi:
                direct = citation._bib_paper(entries[0])
        papers = [direct] if direct else citation.search_papers(query, max_results=limit)
        found = [
            {"title": p.title, "authors": p.authors, "year": p.year, "doi": p.doi,
             "abstract": (p.abstract or "")[:2000], "bibtex": p.bibtex or citation.fetch_bibtex(p)}
            for p in papers[:limit]
        ]
        result = {"papers": found}
        if not args.get("add_to_bibliography"):
            return result
        matching = [p for p in found if (doi and citation._doi_id(p["doi"]) == doi)
                    or (not doi and citation._normalize_title(p["title"]) == wanted)]
        if len(matching) != 1:
            return {**result, "saved": False, "next": "Select one work by its exact title or DOI to add it."}
        official = matching[0]["bibtex"] or ""
        records = citation.parse_bib_string(official)
        if len(records) != 1:
            return {**result, "saved": False, "next": "No single official BibTeX record was available for this work."}
        entry = records[0]
        expected_doi = doi or citation._doi_id(matching[0]["doi"])

        def same_work(fields):
            resolved = citation._doi_id(fields.get("doi"))
            if expected_doi and resolved:
                return expected_doi == resolved
            return citation._normalize_title(fields.get("title", "")) == citation._normalize_title(matching[0]["title"])

        if not same_work(entry["fields"]) or not re.fullmatch(r"[A-Za-z0-9_./:+-]+", entry["key"]):
            return {**result, "saved": False, "next": "The returned BibTeX record could not be bound to this work."}
        bib = self.latex_dir / "references.bib"
        if bib.resolve() != bib:
            raise ValueError("The bibliography must be a regular project file.")
        with bib.open("a+", encoding="utf-8") as output:
            fcntl.flock(output, fcntl.LOCK_EX)
            output.seek(0)
            existing = citation.parse_bib_string(output.read())
            canonical = citation._bib_paper(entry)
            same_title = [r for r in existing if same_work(r["fields"])]
            key = next((r["key"] for r in same_title
                        if (citation._doi_id(r["fields"].get("doi")) or r["fields"].get("author"))
                        and citation._same_paper(r["fields"].get("title", ""), r["fields"], [canonical])), None)
            if key is None:
                conflicts = [r["key"] for r in same_title]
                if conflicts:
                    result["check_existing_keys"] = conflicts
                    if expected_doi and any(citation._doi_id(r["fields"].get("doi")) == expected_doi for r in same_title):
                        return {**result, "saved": False, "next": "The selected DOI is already registered with conflicting "
                                "metadata. Check the listed entries against the returned official BibTeX before citing."}
                key = entry["key"]
                if any(r["key"] == key for r in existing):
                    return {**result, "saved": False, "next": f"Bibliography key {key} already names another work."}
                output.write("\n" + official.strip() + "\n")
                output.flush()
        result.update(saved=True, cite_key=key, cite=r"\cite{" + key + "}", bibliography="paper/references.bib")
        # Put the primary passage beside the cite key, before the author writes a claim.
        source = self._source_record({**entry, "key": key})
        if source:
            record = _load(self.root / source)
            result.update(source=source, source_kind=record["source"], passage=record["passage"][:1600])
        else:
            result["next"] = "Bibliographic identity saved. Inspect the original work before attributing a finding to it."
        return result

    def _source_record(self, entry):
        fields = entry["fields"]
        title = fields.get("title", "").replace("{", "").replace("}", "").strip()
        if not title:
            return None
        doi = citation._doi_id(fields.get("doi"))
        record = (self.state.get("survey_originals") or {}).get(doi)
        if not record and doi.startswith("10.48550/arxiv."):
            record = literature_evidence.doi_original(doi)
        if record and citation.title_similarity(title, record["title"]) < citation._SIMILARITY_THRESHOLD:
            record = None
        if doi and not record:
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
        record = {**record, "bibtex_key": entry["key"]}
        name = hashlib.sha256(record["url"].encode()).hexdigest()[:24] + ".txt"
        path = self.root / "sources" / name
        if path.resolve() != path:
            raise ValueError("The primary source must be a regular project file.")
        content = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
        if not path.is_file() or path.read_text() != content:
            _atomic_write_text(path, content)
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

    def _research_citations(self):
        if (self.config.get("layout_mode") != "strict" or self.config.get("skip_deep_research") is not False
                or deep_research.research_report_status(self.state_dir) != "verified"):
            return None
        bib = self.latex_dir / "references.bib"
        proof = _load(self.state_dir / "native_citations.json", {})
        if not bib.is_file() or proof.get("sha256") != _sha(bib):
            return None
        report = self.state_dir / "deep_research.md"
        trace = literature_evidence.body_source_trace(self.latex_dir, report, proof.get("checked", {}))
        if trace and trace["verified_works"] < 2:
            # Report location is evidence of discovery, not authority for the paper's findings.
            cited_lines = {r.get("report_line") for r in trace["body_citations"].values()}
            used = {citation._doi_id(row["fields"].get("doi")) for row in citation.parse_bib(str(bib))
                    if row["key"] in trace["body_citations"]}
            originals = self.state.get("survey_originals") or {}
            leads = []
            for lead in literature_evidence.source_leads(report.read_text()):
                doi = citation._doi_id(lead["query"])
                if (doi in used if doi else lead["report_line"] in cited_lines):
                    continue
                original = originals.get(doi, {})
                leads.append({**lead, **({"official_title": original["title"],
                                          "abstract_excerpt": original["passage"][:360]} if original else {})})
            trace["unused_leads"] = sorted(leads, key=lambda lead: "official_title" not in lead)[:4]
        return trace

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
        sources = dict(citation.active_tex_sources(self.latex_dir))
        used = {file.relative_to(self.latex_dir).as_posix() for file in sources}
        caption = r"\caption{Recorded experimental measurements.}\label{tab:i2p-measurements}"
        legacy_start = "\n".join([r"\begin{table*}[t]", r"\centering", r"\small", caption])
        active = [row for row in tables.values() if row["path"] in used]
        latest = next(reversed(tables.values()))
        if active:
            # Only migrate the exact wrapper the host previously emitted.
            for row in active:
                old = "\n".join([legacy_start, r"\input{" + row["path"] + "}", r"\end{table*}"])
                if old in sources.get(source, "") and tex.count(old) == 1:
                    latest = row
                    break
            else:
                return
        path = self.latex_dir / latest["path"]
        measurement = self.state.get("experiment") or {}
        if (
            not path.is_file()
            or _sha(path) != latest["sha256"]
            or latest["input_sha256"] != measurement.get("sha256")
        ):
            return
        include = r"\input{" + latest["path"] + "}"
        table = "\n".join([
            r"\begingroup",
            r"\newsavebox{\iipMeasuredBox}",
            r"\sbox{\iipMeasuredBox}{\small" + include + "}",
            r"\def\iipMeasuredTable#1#2{\begin{#1}[#2]",
            r"\centering",
            r"\small",
            caption,
            r"\usebox{\iipMeasuredBox}",
            r"\end{#1}}",
            r"\ifdim\wd\iipMeasuredBox>\columnwidth",
            r"  \iipMeasuredTable{table*}{t}",
            r"\else",
            r"  \iipMeasuredTable{table}{!htbp}",
            r"\fi",
            r"\endgroup",
        ])
        if active:
            _atomic_write_text(source, tex.replace(old, table, 1))
            return
        # Preserve authored floats that reuse a managed label. Restore an
        # input only when its removal is the sole change to a known wrapper.
        occupied = re.compile(r"\\label\s*\{(?:tab:i2p-measurements|app:i2p-measured-results)\}")
        if any(occupied.search(text) for text in sources.values()):
            legacy_empty = re.search(re.escape(legacy_start) + r"\s*\\end\{table\*\}", tex)
            empty_blocks = [table.replace(include, "")]
            if legacy_empty:
                empty_blocks.append(legacy_empty[0])
            for empty in empty_blocks:
                if tex.count(empty) == 1 and empty in sources.get(source, ""):
                    _atomic_write_text(source, tex.replace(empty, table, 1))
                    break
            return
        table = "\n" + table + "\n"
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
        if previous and self.config.get("layout_mode") == "strict" and self.config.get("skip_deep_research") is False:
            managed = r"\\(?:bibliography\s*\{|printbibliography\b)"
            active = re.sub(r"(?<!\\)%[^\n]*", "", content)
            if (re.search(managed, previous.decode("utf-8", errors="replace"))
                    and not re.search(managed, active)
                    and (re.search(r"\\begin\s*\{thebibliography\}", active)
                         or (re.search(r"\\section\*?\s*\{(?:References|Bibliography)\}", active, re.I)
                             and re.search(r"(?:^|\n)\s*\[\d+\]", active)))
                    and not re.search(r"\\(?:input|include)\s*\{[^{}]*(?:bib|ref)[^{}]*\}", active, re.I)):
                return {"ok": False, "checked": True, "diagnostic":
                        "This edit replaces the venue bibliography with hand-numbered references. Keep its "
                        "bibliography command and style. Register the selected works with lookup_papers "
                        "(add_to_bibliography=true) and cite their returned keys in the supported passages."}
        pdf = self.latex_dir / "main.pdf"
        receipt = self.state.get("compile") or {}
        digest = self.state.get("compiled_pdf_sha256")
        if (
            previous is None or receipt.get("ok") is not True or not digest or receipt.get("pdf_sha256") != digest
            or pdf.is_symlink() or not pdf.is_file() or _sha(pdf) != digest
        ):
            return {"ok": True, "checked": False, "reason": "No verified prior build."}
        try:
            strict = self.config.get("layout_mode") == "strict"
            trial = _manuscript_first_pass(self.latex_dir, candidate, check_headings=strict,
                                           page_target=(self.config.get("venue_pages") or 4) if strict else None)
            if "diagnostic" not in trial:
                return {"ok": True, "checked": True, **trial}
            baseline = _manuscript_first_pass(self.latex_dir, previous, check_headings=strict)
        except subprocess.TimeoutExpired:
            return {"ok": False, "checked": False, "diagnostic": "The manuscript check timed out after 90 seconds."}
        except OSError as exc:
            return {"ok": False, "checked": False, "diagnostic": f"The manuscript check could not run: {exc}"}
        if "diagnostic" in baseline:
            return {
                "ok": True, "checked": False,
                "reason": "The current manuscript also fails its first TeX pass. Incremental repair is allowed.",
                "diagnostic": trial["diagnostic"],
                "baseline_diagnostic": baseline["diagnostic"],
            }
        return {"ok": False, "checked": True, "diagnostic": trial["diagnostic"]}

    def compile_paper(self, args):
        source = self.latex_dir / "main.tex"
        result = self._compile_paper(args)
        if not source.is_file() or source.resolve() != source:
            return result
        return {**result, "manuscript_revision": {
            "path": "paper/main.tex", "sha256": _sha(source),
            "next": "Use this post-build revision for the next edit. Read the current sections before a full rewrite.",
        }}

    def _compile_paper(self, args):
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
                # Even an unchanged PDF may gain a recovered primary source or a new survey.
                cached = self.state["compile"]
                trace = self._research_citations()
                if trace != cached.get("research_citations"):
                    if trace:
                        cached["research_citations"] = trace
                    else:
                        cached.pop("research_citations", None)
                    self.save()
                # Recheck editorial observations even when resuming an older compiled checkpoint.
                notes = list(dict.fromkeys([*cached.get("editorial_notes", []), *self._body_repetition_notes()]))
                issues = self._strict_paper_issues()
                checks = cached.get("checks", [])
                final_log = self.latex_dir / "main.log"
                if final_log.is_file():
                    checks = [check for check in checks if check["check"] != "cross_references_resolved"]
                    if warning := delivery_contract.cross_reference_warning(final_log.read_text(errors="replace")):
                        checks.append(dict(check="cross_references_resolved", status="violated", detail=warning))
                if (notes != cached.get("editorial_notes") or issues != cached.get("manuscript_issues")
                        or checks != cached.get("checks")):
                    cached.update(editorial_notes=notes, manuscript_issues=issues, checks=checks)
                    self.save()
                return cached
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
        result["editorial_notes"].extend(self._body_repetition_notes())
        if citation_proof:
            result["unverified_citations"] = citation_proof["unconfirmed"]
        if trace := self._research_citations():
            result["research_citations"] = trace
        self.state.update(compiled_sources=digest, compile=result, compiled_pdf_sha256=result["pdf_sha256"])
        result["manuscript_issues"] = self._strict_paper_issues()
        self.save()
        if result["overflow"] and not getattr(self, "_layout_retry", False):
            self._fit_overfull_tables()
            if digest != _paper_sources(self.latex_dir):
                self._layout_retry = True
                try:
                    return self._compile_paper(args)
                finally:
                    self._layout_retry = False
        self.event("writing", "paper_compiled", f"Rendered {pages} pages.", artifact="paper/main.pdf")
        return result

    def _snapshot(self):
        required = ["manuscript", "experiment"]
        if self.config.get("layout_mode") == "strict":
            required.extend(("citations", "visual"))
        expected = (self.state.get("plan") or {}).get("expected")
        return review.build_snapshot(
            self.root,
            target_track=str(self.config.get("venue") or self.config.get("venue_format") or ""),
            required=required,
            expected_measurement=expected,
            measurement_comparison=self.state.get("measurement_comparison"),
            measurement_run=self._measurement_receipt() if expected else self.state.get("experiment"),
            registered_plots={name: row["script"] for name, row in (self.state.get("plots") or {}).items()
                              if row.get("script") and row.get("status") == "completed"},
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

    def _plot_caption_issues(self):
        """Identify an explicit uncertainty claim for a verified plain bar chart."""
        plots = self.state.get("plots", {})
        # Only this audited renderer draws the selected y values with ax.bar and
        # no uncertainty layer. A future renderer must establish its own semantics.
        template_digest = hashlib.sha256(native_experiment.MEASURED_PLOT.encode()).hexdigest()
        if not plots or template_digest != native_experiment.BAR_WITHOUT_ERROR_BARS_TEMPLATE_SHA256:
            return []
        inputs = set(self.state.get("compile", {}).get("inputs", []))
        bars = {name: row for name, row in plots.items()
                if re.fullmatch(r"[\w-]+\.png", name, re.ASCII) and "figures/" + name in inputs
                and isinstance(row, dict) and isinstance(row.get("plot_spec"), dict)
                and row["plot_spec"].get("kind") == "bar" and row.get("script") is None
                and row.get("status") == "completed" and row.get("plot_protocol") == 1
                and row.get("input_access") == "worker_observed_read" and row.get("plot_filename") == name
                and row.get("artifact") == "paper/figures/" + name}
        if not bars:
            return []
        claim = re.compile(
            r"(?:^|(?<=[.!?]))\s*((?:The )?error bars (?:indicate|represent|denote|show) "
            r"(?:the )?(?:variance|variability|standard deviation|standard error|95% confidence intervals?)"
            r"(?: (?:across|over|between) (?:the )?(?:(?:test|random) )?(?:splits|seeds|runs|trials|folds|replicates))?)"
            r"(?=\s*(?:\.|$))", re.I)
        sources = dict(citation.active_tex_sources(self.latex_dir))
        if self.latex_dir / "main.tex" not in sources:
            return []
        _, figures = body_sections_and_figures(self.latex_dir, sources)
        candidates = {}
        for figure in figures:
            name = Path(figure.asset).name
            if name not in bars or figure.asset not in (name, "figures/" + name):
                continue
            caption = " ".join(figure.caption.replace(r"\%", "%").split())
            ambiguous = re.search(r"\b(?:panels?|no|none|not|without|omit(?:ted)?|absent|would|could)\b|\([a-d]\)",
                                  caption, re.I)
            if (m := claim.search(caption)) and not ambiguous:
                candidates[figure] = m[1]
        if not candidates:
            return []

        import fitz
        from PIL import Image

        notes = []
        with fitz.open(self.latex_dir / "main.pdf") as doc:
            for figure, number, page, text, pixels in rendered_body_images(doc, candidates, sources, self.latex_dir):
                name = Path(figure.asset).name
                row = bars[name]
                identity = re.fullmatch(r"i2p-([0-9a-f]{24})", row.get("identity") or "")
                if not identity or candidates[figure] not in " ".join(text.split()):
                    continue
                receipt = self.state_dir / "native_runs" / identity[1] / "receipt.json"
                png = self.figures_dir / name
                code = native_experiment.measured_plot_code(row["plot_spec"], name)
                if (receipt.resolve() != receipt or _load(receipt) != row or png.resolve() != png
                        or not png.is_file() or _sha(png) != row.get("sha256")
                        or hashlib.sha256(code).hexdigest() != row.get("code_sha256")):
                    continue
                with Image.open(png) as image:
                    # MuPDF hashes decoded samples; the worker's SHA-256 above binds the file bytes.
                    if hashlib.md5(image.convert("RGB").tobytes(), usedforsecurity=False).digest() != pixels:
                        continue
                notes.append(f'Figure {number} (PDF p. {page}, {figure.caption_location}, {name}) says '
                             f'“{candidates[figure]}”. The verified bar renderer draws each selected y value '
                             'without error bars. Correct the caption or regenerate the plot with measured uncertainty.')
        return notes

    def _strict_paper_issues(self):
        if self.config.get("layout_mode") != "strict":
            return []
        rendered = self.state.get("compile", {})
        issues = self._implementation_claims() + self._plot_caption_issues()
        issues.extend(self._body_repetition_notes(empty_headings_only=True))
        main = self.latex_dir / "main.tex"
        if main.is_file() and main.resolve() == main:
            issues.extend(review.reported_change_conflicts(main.read_text()))
            if (self.config.get("skip_deep_research") is False and not rendered.get("cited")
                    and not citation.cited_keys_in_dir(self.latex_dir)):
                issues.append("The body has no linked citations from the requested literature review. "
                              "Use lookup_papers with an exact title or DOI and add_to_bibliography=true. "
                              "Cite its returned key at the supported claim and use the venue bibliography "
                              "instead of typing a numbered reference list.")
        trace = rendered.get("research_citations") or {}
        if (self.config.get("venue_format") == "icml" and float(self.config.get("venue_pages") or 4) >= 4
                and trace and trace.get("verified_works", trace["matched"]) < 2):
            leads = ", ".join(lead["query"] for lead in trace.get("unused_leads", [])[:3])
            issues.append("The empirical related work needs a second distinct research-report paper linked in the body. "
                          "Inspect the returned unused_leads and compare the original abstract to your claim. "
                          "Register a relevant work with lookup_papers(add_to_bibliography=true). "
                          + (f"Survey candidates: {leads}. " if leads else "") +
                          "Use the returned cite key in the supported passage.")
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
            elif row.get("script"):
                script = self.root / row["script"]
                if (not script.is_relative_to(self.root / "scripts") or script.resolve() != script
                        or not script.is_file() or _sha(script) != row.get("code_sha256")):
                    issues.append(f"The plotting script for {name} differs from the one used by its worker. "
                                  "Restore the executed source or regenerate the figure before review.")
        tables = {row["path"]: row for row in self.state.get("tables", {}).values()}
        hand_measured = False
        used = []
        verified_tables = []
        authored_sources = {}
        data = None
        for name in rendered.get("inputs", []):
            path = self.latex_dir / name
            if path.suffix != ".tex" or not path.is_file() or path.resolve() != path:
                continue
            if name in tables:
                used.append(name)
                table = tables[name]
                if _sha(path) != table["sha256"] or table["input_sha256"] != measurement.get(
                    "sha256"
                ):
                    issues.append(f"Regenerate {name} from the current worker results.")
                elif table.get("columns"):
                    columns = table["columns"]
                    if data is None:
                        data, _ = self._measurements()
                    pointer = table.get("rows_pointer", "")
                    try:
                        expected = measurement_table.with_row_identifiers(data, pointer, columns)
                        regenerated = measurement_table.render(data, pointer, columns)
                    except ValueError:
                        issues.append(f"Regenerate {name} with a valid selection of worker rows and columns.")
                        continue
                    if columns != expected:
                        missing = [c["path"] for c in expected if c not in columns]
                        issues.append(f"Rows in {name} omit their measured identifiers ({', '.join(missing)}). "
                                      "Regenerate this table with render_results_table so each row can be traced to its condition.")
                    elif regenerated == path.read_text():
                        verified_tables.append(table)
                    else:
                        issues.append(f"Regenerate {name} to match its recorded worker rows and columns.")
                continue
            source = re.sub(r"(?m)(?<!\\)%.*$", "", path.read_text())
            authored_sources[name] = source
            for block in re.findall(
                r"\\begin\{tabular\*?\}(.*?)\\end\{tabular\*?\}", source, flags=re.S
            ):
                # Consecutive indexed observations are particularly vulnerable to a
                # transcription error when only the beginning of a result fits a chat turn.
                if len(re.findall(r"(?m)^\s*\d+\s*&[^\n]*\d[^\n]*&[^\n]*\d", block)) >= 2:
                    hand_measured = True
        if tables and not used:
            latest = next(reversed(tables))
            issues.append(f"The verified worker table {latest} is missing from the compiled manuscript. "
                          f"Include \\input{{{latest}}} in the appropriate table float. Keep one caption and label.")
        elif hand_measured and not used and measurement_table.describe(self._measurements()[0]):
            issues.append(
                "A numbered results table has manually entered measurements. Use render_results_table "
                "to select the matching worker rows and columns, then replace the tabular block with the returned input."
            )
        issues.extend(measurement_table.transcription_issues(data, verified_tables, authored_sources,
                                                             self.latex_dir / "main.pdf"))
        return issues

    def _opening_review_evidence(self, snapshot, trace=None, *, previous=None, pages=0):
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
        measured_method = snapshot.get("measurement_method", {})
        protocol = [key for key in (measured_method.get("request"), measured_method.get("artifact")) if key]
        citations = _load(self.state_dir / "native_citations.json", {}).get("checked", {})
        body_records = (trace or {}).get("body_citations", {}).values()
        source_paths = [record.get("source") for record in body_records]
        # Prefer the originals behind the author's actual body citations, including an independently found work.
        first_from_each = [next((row["source"] for row in body_records if row.get("source")
                                 and ("report_line" in row) == matched), None) for matched in (True, False)]
        originals = select([*first_from_each, *source_paths, *(r.get("source") for r in citations.values()), *sorted(assets)],
                           "primary_source", 2)
        # PDFs need a page selector. The reviewer can request them through read_evidence.
        comparison = select([(snapshot.get("measurement_comparison") or {}).get("previous")], "supporting_measurement", 1)
        opening = [path for path in dict.fromkeys([*protocol, *methods, *measurements, *comparison, *originals])
                   if Path(path).suffix.lower() != ".pdf"]
        if conclusion := snapshot.get("conclusion"):
            opening.append({key: conclusion[key] for key in ("artifact", "page", "start")})
        if attribution := snapshot.get("evidence_scope", {}).get("attribution"):
            location = {key: attribution[key] for key in ("artifact", "page", "start")}
            if location not in opening:
                opening.append(location)
        # Read current text at the last review's disputed locations. Page images cannot verify quotations.
        if pages and previous and previous.get("revision") != snapshot["revision"]:
            pdf_key = f"{snapshot.get('paper_dir', 'paper')}/main.pdf"
            prior_pages = dict.fromkeys(
                ref["page"] for issue in previous.get("issues", []) if issue.get("status") != "resolved"
                for ref in issue.get("evidence", []) if ref.get("artifact") == pdf_key and ref.get("quote")
                and type(ref.get("page")) is int and 1 <= ref["page"] <= pages
            )
            for page in list(prior_pages)[:2]:
                if conclusion and conclusion["page"] == page and conclusion["start"] == 1:
                    continue
                opening.append({"artifact": pdf_key, "page": page})
        return opening

    def review_snapshot(self, args):
        self._check_compiled()
        snapshot = self._snapshot()
        worker = snapshot.get("worker_measurement")
        if args.get("preflight") and worker and not worker["verified"]:
            return {"blockers": [f"Complete or recover the planned worker output {worker['expected']} before review."]}
        trace = self._research_citations()
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
                return {"blockers": blockers, **({"research_citations": trace} if trace else {})}
        self.snapshot = snapshot
        previous = _load(self.state_dir / "native_review.json")
        self.review_receipts = []
        self.control_attempts = 0
        self.reproduction_attempts = 0
        self.reproduction_format_retry = False
        archive = self.state_dir / "reviews" / snapshot["revision"]
        archive.mkdir(parents=True, exist_ok=True)
        self.reproduction_path = None
        self.reproduction_decision = None
        self.scope_path = None
        self.scope_decision = None
        if args.get("session_id"):
            session_key = hashlib.sha256(_text(args, "session_id", limit=160).encode()).hexdigest()
        if snapshot.get("evidence_scope") and args.get("session_id"):
            self.scope_path = archive / ("scope-" + session_key + ".json")
            self.scope_decision = _load(self.scope_path)
        if snapshot.get("measurement_reproduction") and args.get("session_id"):
            self.reproduction_path = archive / ("reproduction-" + session_key + ".json")
            self.reproduction_decision = _load(self.reproduction_path)
            if self.reproduction_decision and "receipt" in self.reproduction_decision:
                self.review_receipts.append(self.reproduction_decision["receipt"])
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
            "opening_evidence": self._opening_review_evidence(snapshot, trace, previous=previous, pages=pages),
            "method_control": bool(snapshot.get("method_control")),
            "measurement_reproduction": bool(snapshot.get("measurement_reproduction")),
            "evidence_refs": sorted(snapshot["artifacts"]),
        }
        if pages and (notes := self._body_repetition_notes()):
            result["brief"] += ("\nTypesetting observations on the submitted revision. Open the named pages and "
                                "decide whether the repetition is justified or requires a repair.\n"
                                + json.dumps(notes, ensure_ascii=False))
        if pages and (notes := self._plot_caption_issues()):
            result["brief"] += ("\nPlot caption checks. Inspect the named PDF page and caption before reporting a visual finding.\n"
                                + json.dumps(notes, ensure_ascii=False))
        if snapshot.get("measurement_reproduction"):
            result["reproduction_brief"] = review.reproduction_brief(snapshot)
        if snapshot.get("evidence_scope"):
            result["scope_brief"] = review.evidence_scope_brief(snapshot)
            if snapshot.get("method_control") and snapshot["evidence_scope"].get("version") == 4:
                result["inference_test"] = True
            if snapshot["evidence_scope"].get("version") in (3, 4):
                result["claim_instructions"] = review.claim_instructions(snapshot)
            result["brief"] += ("\nRevisit your independent prediction of evidential scope. Check every part "
                                "of the strongest conclusion against the observations. If additional evidence "
                                "changes your earlier judgment, cite the observation that distinguishes the alternatives.")
        if trace:
            result["brief"] += (
                "\nCitation locations from the frozen body PDF, verified survey and registered source files. "
                "Use the pages and source paths to inspect whether the original work supports the manuscript claim. "
                "A missing report match alone does not establish a citation error.\n"
                + json.dumps({k: v for k, v in trace.items() if k != "unused_leads"}, ensure_ascii=False)
            )
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
                observation = ("Independent review opened. The reviewer has only the original research request "
                               "and measurement fields." if snapshot.get("measurement_reproduction")
                               else "Independent review opened for this manuscript revision.")
                self.event("review", "review_started", observation, revision=snapshot["revision"])
            elif attempts[session] != snapshot["revision"]:
                raise ValueError("Reviewer session belongs to a different manuscript revision.")
        return result

    def _require_disclosure(self):
        if self.snapshot and self.snapshot.get("measurement_reproduction") and self.reproduction_decision is None:
            raise ValueError("Before opening submission evidence, call reproduce_measurement with an independent "
                             "program derived from the original request or an unavailable reason.")
        if self.snapshot and self.snapshot.get("evidence_scope") and self.scope_decision is None:
            raise ValueError("Commit the independent evidence scope before opening the manuscript.")

    def commit_evidence_scope(self, args):
        if (not self.snapshot or args.get("revision") != self.snapshot["revision"]
                or not self.snapshot.get("evidence_scope") or not self.scope_path):
            raise ValueError("Open the current independent reviewer session before assessing the evidence scope.")
        if self.snapshot.get("measurement_reproduction") and self.reproduction_decision is None:
            raise ValueError("Complete the blind reproduction decision first.")
        note = {key: _text(args, key, limit=1200) for key in ("comparison", "alternative", "discriminator")}
        if self.scope_decision is None:
            _json(self.scope_path, note)
            self.scope_decision = note
            self.event("review", "evidence_scope_committed",
                       "The reviewer recorded the comparison and a discriminating observation before reading the paper.",
                       revision=self.snapshot["revision"])
        elif self.scope_decision != note:
            raise ValueError("The independent reviewer has already committed the original evidence scope.")
        return self.scope_decision

    def _seal_reproduction(self, decision):
        if self.reproduction_path:
            _json(self.reproduction_path, decision)
        self.reproduction_decision = decision
        if receipt := decision.get("receipt"):
            self.review_receipts.append(receipt)
        return decision

    def review_evidence(self, args):
        if not self.snapshot or args.get("revision") != self.snapshot["revision"]:
            raise ValueError("Open the current review snapshot before requesting evidence.")
        self._require_disclosure()
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
            find=args.get("find"),
        )
        if receipt := result.get("receipt"):
            self.review_receipts.append(receipt)
        return result

    def run_method_control(self, args):
        snap = self.snapshot
        if not snap or args.get("revision") != snap["revision"] or not snap.get("method_control"):
            raise ValueError("Open the frozen worker's review packet before requesting a numerical control.")
        self._require_disclosure()
        method = snap["measurement_method"]
        data, _ = review._artifact(snap, method["artifact"])
        if self.control_attempts >= 4:
            raise ValueError("The review has used its four control attempts. Report the available evidence.")
        self.control_attempts += 1
        self.event("review", "method_control_started", "Reviewer is testing a prediction against the frozen experiment.")
        try:
            result = review_control.run(data, snap["method_control"], args)
        except Exception:
            self.event("review", "method_control_failed", "The reviewer’s numerical control did not return a measurement.")
            raise
        receipt = dict(control=1, revision=snap["revision"], artifact=method["artifact"], sha256=method["sha256"], **result)
        self.review_receipts.append(receipt)
        self.event("review", "method_control_finished",
                   f"{result['target']}: predicted {result['expected']}, measured {result['observed']}.",
                   revision=snap["revision"])
        return {"receipt": receipt}

    def test_inference(self, args):
        """Evaluate the reviewer's own pair of cases without reading the author's project."""
        snap = self.snapshot
        if (not snap or args.get("revision") != snap["revision"] or not snap.get("method_control")
                or snap.get("evidence_scope", {}).get("version") != 4):
            raise ValueError("Open the current empirical review before testing an inference.")
        self._require_disclosure()
        passage = review.reviewed_claim_passage(snap, args.get("passage"), self.review_receipts)
        case = {key: _text(args, key, limit=1200) for key in ("premise", "outcome", "assumptions")}
        program = _text(args, "program", limit=12000)
        case.update(passage=passage, program=program)
        for receipt in self.review_receipts:
            if receipt.get("control") == 3 and all(receipt.get(k) == v for k, v in case.items()):
                return {"receipt": receipt}
        if self.control_attempts >= 4:
            raise ValueError("The review has used its four control attempts. Report the available evidence.")
        self.control_attempts += 1
        self.event("review", "inference_test_started", "Reviewer is testing two cases against a manuscript claim.",
                   revision=snap["revision"])
        try:
            outputs = {label: review_control.reproduce(snap["method_control"], program, {"case":label}, ["premise", "outcome"])
                       for label in ("A", "B")}
        except Exception:
            self.event("review", "inference_test_failed", "The comparison did not return two numeric cases.",
                       revision=snap["revision"])
            raise
        a, b = outputs["A"], outputs["B"]
        receipt = dict(case, control=3, revision=snap["revision"], cases=outputs,
                       same_premise=a["premise"] == b["premise"],
                       distinguishable_outcome=not math.isclose(a["outcome"], b["outcome"], rel_tol=1e-6, abs_tol=1e-9),
                       outcome_rel_tol=1e-6, outcome_abs_tol=1e-9)
        self.review_receipts.append(receipt)
        self.event("review", "inference_test_finished",
                   f'{case["premise"]}: {a["premise"]}, {b["premise"]}. {case["outcome"]}: {a["outcome"]}, {b["outcome"]}.',
                   revision=snap["revision"])
        return {"receipt": receipt}

    def reproduce_measurement(self, args):
        snap = self.snapshot
        if not snap or args.get("revision") != snap["revision"] or not (task := snap.get("measurement_reproduction")):
            raise ValueError("Open the review packet for the revised measurements first.")
        if not self.reproduction_path:
            return self._reproduce(args, snap, task)
        with self.reproduction_path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.reproduction_decision is None and (stored := _load(self.reproduction_path)):
                self.reproduction_decision = stored
                if receipt := stored.get("receipt"):
                    self.review_receipts.append(receipt)
            return self._reproduce(args, snap, task)

    def _reproduce(self, args, snap, task):
        if ("program" in args) == ("unavailable" in args):
            raise ValueError("Submit one independent program or explain why the calculation is unavailable.")
        program = _text(args, "program", limit=12000) if "program" in args else None
        reason = _text(args, "unavailable", limit=1200) if "unavailable" in args else None
        if self.reproduction_decision is not None:
            sealed = self.reproduction_decision
            if ((program and sealed.get("receipt", {}).get("program") == program)
                    or (reason and sealed.get("unavailable") == reason)):
                return sealed
            raise ValueError("The reviewer already opened the submission. Its independent program is sealed.")
        if reason:
            decision = self._seal_reproduction({"unavailable": reason})
            self.event("review", "measurement_reproduction_unavailable", reason, revision=snap["revision"])
            return decision
        regular_attempts = 3
        attempt_limit = regular_attempts + int(self.reproduction_format_retry)
        if self.reproduction_attempts >= attempt_limit:
            raise ValueError("Independent calculation attempts exhausted. Supply unavailable with the specific reason.")
        data, _ = review._artifact(snap, task["artifact"])
        row = measurement_table.reproduction_row(json.loads(data), snap["measurement_comparison"]["unchanged_arrays"],
                                                 task["sha256"])
        self.reproduction_attempts += 1
        identity = ", ".join(f"{k} {v}" for k, v in task["identifiers"].items()) or "single observation"
        self.event("review", "measurement_reproduction_started", f"Independently recomputing {identity}.",
                   condition=identity, attempt=self.reproduction_attempts)
        try:
            expected = review_control.reproduce(snap["method_control"], program, task["identifiers"], task["columns"])
        except review_control.ReproductionFormatError as error:
            self.reproduction_format_retry = True
            remaining = regular_attempts + 1 - self.reproduction_attempts
            self.event("review", "measurement_reproduction_failed", "The program returned an invalid measurement row.",
                       failure="row_format", attempt=self.reproduction_attempts)
            raise ValueError(f"{error} Remaining executions: {remaining}.") from None
        except Exception:
            self.event("review", "measurement_reproduction_failed", "The independent calculation did not return the requested outcomes.",
                       failure="execution", attempt=self.reproduction_attempts)
            raise
        observed = row["outcomes"]
        differences = [key for key in task["columns"] if not math.isclose(expected[key], observed[key],
                                                                           rel_tol=1e-6, abs_tol=1e-9)]
        receipt = {**task, "control": 2, "revision": snap["revision"], "target": f"Recorded condition {identity}",
                   "backend": snap["method_control"]["backend"], "expected": expected, "observed": observed,
                   "matches": not differences, "differences": differences, "program": program,
                   "rel_tol": 1e-6, "abs_tol": 1e-9,
                   "rationale": "The reviewer independently recomputed all numeric outcomes for this condition from the study protocol."}
        receipt["program_sha256"] = hashlib.sha256(program.encode()).hexdigest()
        decision = self._seal_reproduction({"receipt": receipt})
        description = "Differing outcomes: " + ", ".join(differences) if differences else "All requested outcomes matched."
        detail = {}
        if differences:
            first = differences[0]
            detail = {"metric": first, "independent_value": str(expected[first]), "reported_value": str(observed[first])}
        self.event("review", "measurement_reproduction_finished", f"Recomputed {identity}. {description}",
                   revision=snap["revision"], condition=identity, differed=len(differences),
                   outcomes=len(task["columns"]), **detail)
        return decision

    def check_review(self, args):
        return self.record_review(args, check_only=True)

    def record_review(self, args, *, check_only=False):
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
        try:
            self._require_disclosure()
            if any(r.get("control") and r not in observed for r in self.review_receipts):
                raise ValueError("The verdict must retain all executed numerical controls.")
        except ValueError as exc:
            if check_only:
                return {"valid": False, "diagnostic": str(exc)}
            raise
        try:
            record = review.validate_review(snapshot, args["verdict"], previous=previous, observed=observed)
        except ValueError as exc:
            if check_only:
                feedback = review.draft_evidence_feedback(snapshot, args["verdict"], observed, str(exc))
                return {"valid": False, "diagnostic": feedback}
            raise
        if check_only:
            return {"valid": True}
        record.update(
            session_id=_text(args, "session_id", limit=160),
            paper_pdf_sha256=_sha(self.latex_dir / "main.pdf"),
        )
        if snapshot.get("evidence_scope"):
            record["evidence_scope"] = self.scope_decision
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
            or ("worker_measurement" in snap and (not snap["worker_measurement"]["verified"]
                or measurement.get("artifact") != snap["worker_measurement"]["expected"]))
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
        trace = self._research_citations()
        trace_details = {"research_citations": trace} if trace else {}
        if problems:
            return {"delivered": False, "blockers": problems, **trace_details}
        self.state.update(state="delivered", delivery_revision=snap["revision"],
                          delivery_inputs_sha256=review.delivery_inputs_digest(snap))
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
            **trace_details,
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
            "commit_evidence_scope",
            "review_evidence",
            "run_method_control",
            "test_inference",
            "reproduce_measurement",
            "check_review",
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
