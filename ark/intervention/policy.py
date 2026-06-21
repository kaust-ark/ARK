"""Risk classification for the intervention subsystem.

Pure logic, no side effects: given a shell command (what an agent is about to
run) or a structured orchestrator action, decide whether to allow it, send a
non-blocking notification, ask a human first, or deny it outright.

Design:

- Every detection yields a (``Category``, ``Severity``). Severity is the lever:
  a single ``rm file`` inside the work area is LOW, ``rm -rf`` is HIGH, deleting
  ``.git`` or the project root is CRITICAL.
- The ``autonomy`` level maps the minimum severity that triggers a human ask:
  ``cautious`` asks on anything detected, ``standard`` asks on HIGH+,
  ``autonomous`` only on CRITICAL.
- Categories can be toggled off in config; a disabled category degrades to a
  log-only ``allow`` (the action is still recorded, never silently dropped).

Statefulness (e.g. "more than N jobs in 10 minutes") lives in the caller, which
passes the already-counted ``recent_job_count`` in — this module stays pure.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence


# ── Vocabulary ─────────────────────────────────────────────

class Category:
    DESTRUCTIVE_FS = "destructive_fs"
    BULK_COMPUTE = "bulk_compute"
    CREDENTIALS = "credentials"
    DATA_EXFIL = "data_exfil"
    SPEND = "spend"

    ALL = (DESTRUCTIVE_FS, BULK_COMPUTE, CREDENTIALS, DATA_EXFIL, SPEND)


class Severity:
    """Ordered severity. Higher = more consequential."""
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    _NAMES = {LOW: "low", MEDIUM: "medium", HIGH: "high", CRITICAL: "critical"}

    @classmethod
    def name(cls, level: int) -> str:
        return cls._NAMES.get(level, str(level))


# Action verbs returned in a Decision.
ALLOW = "allow"      # proceed, no human contact (still logged)
NOTIFY = "notify"    # proceed, but send a non-blocking FYI to the human
ASK = "ask"          # block until a human approves/denies
DENY = "deny"        # block and refuse outright


# autonomy level → minimum severity that escalates to a human ASK
_AUTONOMY_ASK_FLOOR = {
    "cautious": Severity.LOW,
    "standard": Severity.HIGH,
    "autonomous": Severity.CRITICAL,
}
_DEFAULT_AUTONOMY = "standard"


@dataclass
class Decision:
    """The verdict for one command/action."""
    action: str                      # ALLOW / NOTIFY / ASK / DENY
    category: Optional[str] = None
    severity: int = Severity.LOW
    reason: str = ""
    detail: str = ""                 # e.g. the matched command, redacted
    # Free-form context surfaced to the human in the approval card.
    consequence: str = ""

    @property
    def blocks(self) -> bool:
        return self.action in (ASK, DENY)


# ── Detection helpers ──────────────────────────────────────

_RM_RECURSIVE_RE = re.compile(r"-[a-zA-Z]*r", )          # -r / -rf / -fr ...
_RM_FORCE_RE = re.compile(r"-[a-zA-Z]*f")
_CLOUD_PROVISION_RE = re.compile(
    r"\b(run-instances|instances\s+create|vm\s+create|compute\s+instances\s+create|"
    r"create-instances?|deployment\s+create)\b"
)
_HTTP_UPLOAD_RE = re.compile(r"(?:^|\s)(-d|--data\b|--data-binary\b|-T\b|--upload-file\b|-X\s*POST\b|-X\s*PUT\b|-F\b)")
_SECRET_PATH_RE = re.compile(
    r"(\.aws/credentials|\.ssh/id_[a-z0-9]+|\.config/gcloud|\.netrc|"
    r"oauth_creds\.json|\.claude\.json|credentials\.json|secrets?\.(ya?ml|json|env))"
)
# Names that look like secrets (used for both detection and redaction). The
# prefix is optional so bare `TOKEN`, `SECRET`, `API_KEY` are caught too — not
# just `HF_TOKEN` / `OPENAI_API_KEY`.
_SECRET_ENVNAME_RE = re.compile(
    r"\b[A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)\b"
)
# An inline assignment of a secret-looking name to a literal value.
_SECRET_ASSIGN_RE = re.compile(
    r"\b([A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?))\s*=\s*([^\s'\"]{6,})"
)


def redact_secrets(text: str) -> str:
    """Replace the *value* of any ``SECRETNAME=value`` with ``***``.

    Shared by the observability layer so a hardcoded key never reaches a log
    line, the step journal, or the approval audit trail.
    """
    if not text:
        return text
    return _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=***", text)

_JOB_SUBMIT_EXES = frozenset({"sbatch", "srun", "qsub", "bsub"})
_CLOUD_EXES = frozenset({"aws", "gcloud", "az", "gsutil"})
_EXFIL_EXES = frozenset({"scp", "rsync", "sftp"})
_DELETE_EXES = frozenset({"rm", "rmdir", "shred", "unlink"})

# Hosts that are not "external" for exfil purposes.
_LOCAL_HOST_RE = re.compile(r"^(localhost|127\.0\.0\.1|0\.0\.0\.0|::1)$")


def _basename(arg: str) -> str:
    return Path(arg).name


def _looks_like_remote(token: str) -> bool:
    """scp/rsync remote spec ``user@host:/path`` or ``host:/path``."""
    if "://" in token:
        return True
    m = re.match(r"^(?:[^@/]+@)?([^@:/]+):", token)
    if not m:
        return False
    host = m.group(1)
    return not _LOCAL_HOST_RE.match(host)


def _path_outside(target: str, work_dirs: Sequence[Path]) -> bool:
    """True if ``target`` resolves outside every configured work dir."""
    if not work_dirs:
        return False
    if target.startswith("-"):
        return False
    try:
        p = Path(os.path.expanduser(target))
        if not p.is_absolute():
            # Relative paths are resolved against cwd by the caller's work dir;
            # treat plain relative names as inside.
            if ".." not in Path(target).parts:
                return False
            p = (work_dirs[0] / target).resolve()
        else:
            p = p.resolve()
    except Exception:
        return False
    for w in work_dirs:
        try:
            p.relative_to(Path(w).resolve())
            return False
        except ValueError:
            continue
    return True


def _is_protected_path(target: str) -> bool:
    """Deleting these is always CRITICAL regardless of location."""
    name = target.rstrip("/").split("/")[-1] if target else ""
    if name in (".git", ".env", ".conda_env"):
        return True
    # root-ish / home-ish absolute deletions
    norm = target.rstrip("/")
    if norm in ("", "/", os.path.expanduser("~"), "/home", "/data", "*"):
        return True
    return False


# ── Policy ─────────────────────────────────────────────────

@dataclass
class InterventionPolicy:
    autonomy: str = _DEFAULT_AUTONOMY
    enabled: set = field(default_factory=lambda: set(Category.ALL))
    # bulk_compute: ask once a burst exceeds this many jobs in the window
    max_jobs_in_window: int = 8
    job_window_seconds: int = 600
    # spend: notify at this cumulative USD, ask (block) at the hard cap
    spend_notify_usd: float = 20.0
    spend_ask_usd: float = 100.0

    def _ask_floor(self) -> int:
        return _AUTONOMY_ASK_FLOOR.get(self.autonomy, Severity.HIGH)

    def _resolve(self, category: str, severity: int, reason: str,
                 detail: str = "", consequence: str = "") -> Decision:
        """Map a (category, severity) detection to an action under the policy."""
        if category not in self.enabled:
            # Category disabled → allow, but keep the category/severity for logging.
            return Decision(ALLOW, category, severity, reason="(category disabled) " + reason,
                            detail=detail, consequence=consequence)
        if severity >= self._ask_floor():
            return Decision(ASK, category, severity, reason, detail, consequence)
        return Decision(ALLOW, category, severity, reason, detail, consequence)

    # ---- shell commands (agent wrappers + circuit breaker) ----

    def classify_command(
        self,
        command,
        *,
        work_dirs: Optional[Sequence[Path]] = None,
        recent_job_count: int = 0,
    ) -> Decision:
        """Classify a shell command. ``command`` may be an argv list or a string.

        ``work_dirs`` are the directories the agent may freely mutate (its code
        dir, results/, paper/). ``recent_job_count`` is how many compute jobs the
        caller has already seen in the rolling window (for burst detection).
        """
        argv = self._as_argv(command)
        if not argv:
            return Decision(ALLOW, reason="empty command")
        work_dirs = [Path(w) for w in (work_dirs or [])]
        exe = _basename(argv[0])
        rest = argv[1:]
        joined = " ".join(argv)

        # 1) Destructive filesystem ---------------------------------------
        if exe in _DELETE_EXES:
            targets = [a for a in rest if not a.startswith("-")]
            recursive = any(_RM_RECURSIVE_RE.search(a) for a in rest if a.startswith("-"))
            forced = any(_RM_FORCE_RE.search(a) for a in rest if a.startswith("-"))
            if any(_is_protected_path(t) for t in targets):
                return self._resolve(Category.DESTRUCTIVE_FS, Severity.CRITICAL,
                                     "delete a protected path (.git / project root / home)",
                                     detail=joined, consequence="Irreversible loss of project history or root data.")
            outside = any(_path_outside(t, work_dirs) for t in targets)
            if outside:
                return self._resolve(Category.DESTRUCTIVE_FS, Severity.HIGH,
                                     "delete files outside the work area",
                                     detail=joined, consequence="Removes files ARK does not own.")
            if recursive and (forced or exe == "rm"):
                return self._resolve(Category.DESTRUCTIVE_FS, Severity.HIGH,
                                     "recursive force delete (rm -rf)",
                                     detail=joined, consequence="Recursively removes a directory tree.")
            if len(targets) > 5:
                return self._resolve(Category.DESTRUCTIVE_FS, Severity.MEDIUM,
                                     f"delete {len(targets)} files at once",
                                     detail=joined)
            return self._resolve(Category.DESTRUCTIVE_FS, Severity.LOW,
                                 "single-file delete in work area", detail=joined)

        # NOTE: agent git operations are intentionally NOT gated — they fired on
        # nearly every agent and were too noisy. ARK still gates its own
        # autonomous push via classify_action("git_push").

        # 2) Bulk compute --------------------------------------------------
        if exe in _JOB_SUBMIT_EXES:
            if recent_job_count + 1 > self.max_jobs_in_window:
                return self._resolve(Category.BULK_COMPUTE, Severity.HIGH,
                                     f"job burst: >{self.max_jobs_in_window} submissions "
                                     f"in {self.job_window_seconds//60}min",
                                     detail=joined,
                                     consequence="Launches many cluster jobs at once.")
            return self._resolve(Category.BULK_COMPUTE, Severity.LOW,
                                 "single job submission", detail=joined)

        if exe in _CLOUD_EXES and _CLOUD_PROVISION_RE.search(joined):
            return self._resolve(Category.BULK_COMPUTE, Severity.HIGH,
                                 "provision a cloud instance (billable)",
                                 detail=joined, consequence="Starts a paid cloud VM.")

        # 3) Credentials ---------------------------------------------------
        if exe == "ark-request-secret":
            name = rest[0] if rest else "?"
            return self._resolve(Category.CREDENTIALS, Severity.HIGH,
                                 f"agent requests secret '{name}'",
                                 detail=joined, consequence="A secret value will be supplied to the run.")
        if _SECRET_PATH_RE.search(joined) and exe in ("cat", "cp", "scp", "rsync", "less", "head", "tail", "base64", "curl", "wget"):
            return self._resolve(Category.CREDENTIALS, Severity.HIGH,
                                 "access a credential/secret file",
                                 detail=joined, consequence="Reads or moves stored secrets.")
        if _SECRET_ASSIGN_RE.search(joined):
            return self._resolve(Category.CREDENTIALS, Severity.HIGH,
                                 "hardcode a secret value inline",
                                 detail=redact_secrets(joined),
                                 consequence="Bakes a credential into a command or file.")
        if exe in ("echo", "printenv", "env") and _SECRET_ENVNAME_RE.search(joined):
            return self._resolve(Category.CREDENTIALS, Severity.MEDIUM,
                                 "print a secret environment variable",
                                 detail=joined, consequence="Could leak a secret into logs.")

        # 4) Data exfiltration (outward) -----------------------------------
        if exe in _EXFIL_EXES and any(_looks_like_remote(a) for a in rest):
            return self._resolve(Category.DATA_EXFIL, Severity.HIGH,
                                 "transfer data to an external host",
                                 detail=joined, consequence="Sends data off-machine.")
        # (agent `git push` intentionally not gated — see note above)
        if exe in ("curl", "wget") and _HTTP_UPLOAD_RE.search(" " + " ".join(rest)):
            # only outward when a remote URL is present
            if any(tok.startswith("http://") or tok.startswith("https://") for tok in rest):
                return self._resolve(Category.DATA_EXFIL, Severity.MEDIUM,
                                     "HTTP upload/POST to a URL",
                                     detail=joined, consequence="Uploads data to a remote URL.")

        return Decision(ALLOW, reason="no rule matched", detail=joined)

    # ---- structured orchestrator actions ----

    def classify_action(self, action_type: str, **ctx) -> Decision:
        """Classify an orchestrator-initiated action.

        Supported ``action_type``:
        - ``cloud_provision`` (ctx: provider, instance_type) → BULK_COMPUTE/HIGH
        - ``git_push``        (ctx: remote, branch)          → DATA_EXFIL/HIGH
        - ``spend``           (ctx: total_usd)               → SPEND/notify|ask
        """
        if action_type == "cloud_provision":
            it = ctx.get("instance_type") or "?"
            prov = ctx.get("provider") or "cloud"
            return self._resolve(Category.BULK_COMPUTE, Severity.HIGH,
                                 f"provision {prov} instance ({it})",
                                 detail=f"{prov}:{it}",
                                 consequence="Starts a paid cloud VM that bills until torn down.")
        if action_type == "git_push":
            remote = ctx.get("remote") or "origin"
            branch = ctx.get("branch") or ""
            return self._resolve(Category.DATA_EXFIL, Severity.HIGH,
                                 f"git push to {remote} {branch}".strip(),
                                 detail=f"{remote} {branch}".strip(),
                                 consequence="Publishes the project's commits to a remote.")
        if action_type == "spend":
            total = float(ctx.get("total_usd") or 0.0)
            if Category.SPEND not in self.enabled:
                return Decision(ALLOW, Category.SPEND, Severity.LOW, "(spend disabled)")
            if total >= self.spend_ask_usd:
                return Decision(ASK, Category.SPEND, Severity.CRITICAL,
                                f"cumulative spend ${total:.2f} ≥ hard cap ${self.spend_ask_usd:.0f}",
                                detail=f"${total:.2f}",
                                consequence="Run has spent past the hard budget cap.")
            if total >= self.spend_notify_usd:
                return Decision(NOTIFY, Category.SPEND, Severity.MEDIUM,
                                f"cumulative spend ${total:.2f} ≥ ${self.spend_notify_usd:.0f}",
                                detail=f"${total:.2f}")
            return Decision(ALLOW, Category.SPEND, Severity.LOW, f"spend ${total:.2f}")

        return Decision(ALLOW, reason=f"unknown action '{action_type}'")

    @staticmethod
    def _as_argv(command) -> list:
        if isinstance(command, (list, tuple)):
            return [str(c) for c in command]
        if not command:
            return []
        try:
            return shlex.split(str(command))
        except ValueError:
            return str(command).split()


# ── Construction from config ───────────────────────────────

def load_policy(config: Optional[dict]) -> InterventionPolicy:
    """Build an InterventionPolicy from a project config dict.

    Reads the ``intervention`` block; absent keys fall back to safe defaults.
    Example config::

        intervention:
          autonomy: standard          # cautious | standard | autonomous
          categories:                 # toggle individual categories
            destructive_fs: true
            bulk_compute: true
            credentials: true
            data_exfil: true
            spend: true
          max_jobs_in_window: 8
          job_window_seconds: 600
          spend_notify_usd: 20
          spend_ask_usd: 100
    """
    cfg = ((config or {}).get("intervention") or {}) if isinstance(config, dict) else {}
    autonomy = str(cfg.get("autonomy") or _DEFAULT_AUTONOMY).lower()
    if autonomy not in _AUTONOMY_ASK_FLOOR:
        autonomy = _DEFAULT_AUTONOMY

    cats_cfg = cfg.get("categories") or {}
    enabled = {c for c in Category.ALL if cats_cfg.get(c, True)}

    def _num(key, default):
        try:
            return type(default)(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    return InterventionPolicy(
        autonomy=autonomy,
        enabled=enabled,
        max_jobs_in_window=_num("max_jobs_in_window", 8),
        job_window_seconds=_num("job_window_seconds", 600),
        spend_notify_usd=_num("spend_notify_usd", 20.0),
        spend_ask_usd=_num("spend_ask_usd", 100.0),
    )
