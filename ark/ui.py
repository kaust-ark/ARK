"""Lightweight CLI UI helpers for ARK.

No external dependencies — stdlib only. All ANSI output gated on isatty().
"""

import re
import sys
import threading
import time


def _isatty() -> bool:
    """Check if stdout is a terminal."""
    return hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()


# ══════════════════════════════════════════════════════════════
#  ANSI Styles
# ══════════════════════════════════════════════════════════════

class Style:
    """ANSI escape codes."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"

    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"

    # Per-agent colors
    AGENT_COLORS = {
        "reviewer":      "\033[94m",   # Blue
        "planner":       "\033[96m",   # Cyan
        "writer":        "\033[92m",   # Green
        "experimenter":  "\033[95m",   # Magenta
        "researcher":    "\033[95m",   # Magenta
        "visualizer":    "\033[93m",   # Yellow
        "meta_debugger": "\033[91m",   # Red
        "coder":         "\033[92m",   # Green
    }


def styled(text: str, *styles: str) -> str:
    """Wrap text in ANSI styles. Returns plain text if not a terminal."""
    if not _isatty() or not styles:
        return text
    prefix = "".join(styles)
    return f"{prefix}{text}{Style.RESET}"


def agent_styled(agent_type: str, text: str) -> str:
    """Apply agent-specific color."""
    color = Style.AGENT_COLORS.get(agent_type, Style.WHITE)
    return styled(text, color)


_ANSI_RE = re.compile(r'\033\[[0-9;]*m')

def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return _ANSI_RE.sub('', text)


# ══════════════════════════════════════════════════════════════
#  Icons
# ══════════════════════════════════════════════════════════════

class Icons:
    """Unicode icons for CLI output."""

    # Step status
    SUCCESS  = "✓"
    ERROR    = "✗"
    WARNING  = "⚠"
    PROGRESS = "→"
    INFO     = "ℹ"

    # Agent types
    AGENT = {
        "reviewer":      "🔍",
        "planner":       "📋",
        "writer":        "✏️",
        "experimenter":  "🧪",
        "researcher":    "📚",
        "visualizer":    "🖼️",
        "meta_debugger": "🔧",
        "coder":         "⌨️",
    }

    # Project status
    RUNNING = "🟢"
    STOPPED = "⚪"

    # Score trend
    UP   = "↑"
    DOWN = "↓"
    FLAT = "→"

    # Misc
    TIMER     = "⏱️"
    RATELIMIT = "⏳"
    PHASE     = "▸"
    SECTION   = "═"
    SPARKLE   = "✦"
    TELEGRAM  = "📨"

    # Step icons within a phase (for Telegram & CLI)
    PIPELINE_STEP = {
        "Dev":         "🔬",
        "Initialize":  "🚀",
        "Compile":     "📝",
        "Review":      "🔍",
        "Plan":        "📋",
        "Execute":     "⚡",
        "Validate":    "✅",
        "Write":       "✏️",
        "Experiment":  "🧪",
        "Analyze":     "📈",
    }
    # Backward compat alias
    PIPELINE_PHASE = PIPELINE_STEP

    # Wizard step icons (for `ark new`)
    WIZARD_STEP = {
        1: "🗂",   # Code Directory
        2: "🏛",   # Target Venue
        3: "💡",   # Research Idea
        4: "👥",   # Authors
        5: "🖥",   # Experiment Compute
        6: "🤖",   # AI Model
        7: "🎨",   # Figure Generation
        8: "🌐",   # Language
        9: "📨",   # Telegram
    }

    @classmethod
    def for_agent(cls, agent_type: str) -> str:
        return cls.AGENT.get(agent_type, "⚙️")

    @classmethod
    def for_step(cls, status: str) -> str:
        return {
            "info": cls.INFO,
            "success": cls.SUCCESS,
            "warning": cls.WARNING,
            "error": cls.ERROR,
            "progress": cls.PROGRESS,
        }.get(status, cls.INFO)

    @classmethod
    def for_step_header(cls, step_name: str) -> str:
        """Get icon for a pipeline step by matching the first word."""
        for key in cls.PIPELINE_STEP:
            if key.lower() in step_name.lower():
                return cls.PIPELINE_STEP[key]
        return cls.PHASE

    # Backward compat alias
    for_phase = for_step_header

    @classmethod
    def for_wizard_step(cls, step_num: int) -> str:
        return cls.WIZARD_STEP.get(step_num, "▸")


# ══════════════════════════════════════════════════════════════
#  Score Visualization
# ══════════════════════════════════════════════════════════════

_SPARK_CHARS = "▁▂▃▄▅▆▇█"

def score_sparkline(scores: list, width: int = 10) -> str:
    """Generate a sparkline from score history. e.g. '▁▂▃▅▃▅▇'"""
    if not scores:
        return ""
    recent = scores[-width:]
    chars = []
    for s in recent:
        idx = min(7, max(0, int(float(s) * 7 / 10)))
        chars.append(_SPARK_CHARS[idx])
    return "".join(chars)


def score_trend(current: float, previous: float) -> str:
    """Return colored trend indicator: '+0.5 ↑' or '-0.3 ↓'."""
    delta = current - previous
    if delta > 0.3:
        return styled(f"+{delta:.1f} {Icons.UP}", Style.GREEN)
    elif delta < -0.3:
        return styled(f"{delta:.1f} {Icons.DOWN}", Style.RED)
    else:
        return styled(f"{delta:+.1f} {Icons.FLAT}", Style.DIM)


# ══════════════════════════════════════════════════════════════
#  Elapsed Timer (threaded, for agent execution)
# ══════════════════════════════════════════════════════════════

class ElapsedTimer:
    """Shows elapsed time during long-running agent calls on stderr.

    Only active when output is a terminal.

    Usage:
        timer = ElapsedTimer("reviewer")
        timer.start()
        # ... agent runs ...
        elapsed = timer.stop()
    """

    def __init__(self, agent_type: str):
        self._agent_type = agent_type
        self._thread = None
        self._stop = threading.Event()
        self._start_time = 0.0

    def start(self):
        if not _isatty():
            self._start_time = time.time()
            return
        self._start_time = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> float:
        """Stop timer, clear line, return elapsed seconds."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        elapsed = time.time() - self._start_time
        if _isatty():
            sys.stderr.write("\r" + " " * 70 + "\r")
            sys.stderr.flush()
        return elapsed

    def _run(self):
        while not self._stop.wait(timeout=5):
            elapsed = time.time() - self._start_time
            icon = Icons.for_agent(self._agent_type)
            color = Style.AGENT_COLORS.get(self._agent_type, "")
            elapsed_str = _fmt_elapsed(elapsed)
            line = f"\r  {color}{icon} [{self._agent_type}] {elapsed_str}{Style.RESET}"
            sys.stderr.write(line)
            sys.stderr.flush()


# ══════════════════════════════════════════════════════════════
#  Rate Limit Countdown
# ══════════════════════════════════════════════════════════════

class RateLimitCountdown:
    """Blocking countdown during rate limit waits.

    Shows countdown on stderr. Falls back to plain sleep if not tty.
    """

    def __init__(self, wait_seconds: int):
        self._wait = wait_seconds

    def run(self):
        """Block and show countdown."""
        if not _isatty():
            time.sleep(self._wait)
            return

        end_time = time.time() + self._wait
        while True:
            remaining = end_time - time.time()
            if remaining <= 0:
                break
            m, s = divmod(int(remaining), 60)
            sys.stderr.write(f"\r  {Icons.RATELIMIT} Rate limited: resuming in {m}m {s:02d}s  ")
            sys.stderr.flush()
            time.sleep(1)

        sys.stderr.write("\r" + " " * 60 + "\r")
        sys.stderr.flush()


# ══════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════

def _fmt_elapsed(seconds: float) -> str:
    """Format seconds as 'Xm Ys' or 'Ys'."""
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_status_line(agent_type: str, status: str, elapsed: float = 0) -> str:
    """Format a status line like: '🔍 [reviewer] ✓ completed (45s)'"""
    icon = Icons.for_agent(agent_type)
    step_icon = Icons.for_step(status)
    color = Style.AGENT_COLORS.get(agent_type, "")
    elapsed_str = f" ({_fmt_elapsed(elapsed)})" if elapsed else ""
    text = f"{icon} [{agent_type}] {step_icon} {status}{elapsed_str}"
    return styled(text, color)
