#!/usr/bin/env python3
"""
ARK CLI - Automatic Research Kit command-line interface

Commands:
    ark new <project>       Create a new research project (interactive wizard)
    ark create <project>    Alias for 'ark new'
    ark run <project>       Run the orchestrator in the background
    ark status [project]    Show project status summary
    ark monitor <project>   Live-monitor a running project
    ark update <project>    Send updates to a running project
    ark stop <project>      Stop a running project
    ark delete <project>    Delete a project
    ark list                List all projects
"""

import argparse
import os
import re
import signal
import shutil
import subprocess
import sys
import textwrap
import time
import yaml
from datetime import datetime
from pathlib import Path


# ============================================================
#  Path discovery
# ============================================================

from ark.paths import get_ark_root, get_config_dir


def get_projects_dir() -> Path:
    root = get_ark_root()
    pdir = root / "projects"
    pdir.mkdir(exist_ok=True)
    return pdir


def _get_configured_model(default: str = "claude-sonnet-4-6") -> str:
    """
    Return the default ARK model.

    No global config fallback — ARK is multi-tenant; per-project config.yaml
    must declare its own model. Callers needing a project-specific model
    should read it from the project's own config.yaml.
    """
    return default


def get_project_config(name: str) -> dict:
    """Load a project's config.yaml."""
    config_file = get_projects_dir() / name / "config.yaml"
    if not config_file.exists():
        return {}
    with open(config_file) as f:
        return yaml.safe_load(f) or {}


def ensure_project_symlinks(project_dir: Path, code_dir: str):
    """Create a symlink to code_dir under projects/<name>/.

    Created links:
        projects/<name>/workspace -> <code_dir>
    """
    project_dir = Path(project_dir)
    code_path = Path(code_dir)

    # Clean up old "code" symlink (renamed to workspace)
    old_link = project_dir / "code"
    if old_link.is_symlink():
        old_link.unlink()

    ws_link = project_dir / "workspace"
    if code_path.exists() and not ws_link.exists():
        ws_link.symlink_to(code_path)
    elif code_path.exists() and ws_link.is_symlink() and ws_link.resolve() != code_path.resolve():
        ws_link.unlink()
        ws_link.symlink_to(code_path)


# ============================================================
#  Venue data
# ============================================================

VENUES = [
    {"name": "NeurIPS",    "format": "neurips",  "pages": 9},
    {"name": "ICML",       "format": "icml",     "pages": 9},
    {"name": "ICLR",       "format": "iclr",     "pages": 9},
    {"name": "ACL",        "format": "acl",       "pages": 8},
    {"name": "EMNLP",      "format": "acl",       "pages": 8},
    {"name": "CVPR",       "format": "cvpr",      "pages": 8},
    {"name": "EuroMLSys",  "format": "sigplan",   "pages": 6},
    {"name": "MLSys",      "format": "mlsys",     "pages": 12},
    {"name": "INFOCOM",    "format": "ieee",      "pages": 9},
    {"name": "OSDI",       "format": "usenix",    "pages": 14},
    {"name": "SOSP",       "format": "sigplan",   "pages": 15},
]

OTHER_VENUES = [
    {"name": "Course Project",   "format": "article", "pages": 0},
    {"name": "Workshop Paper",   "format": "article", "pages": 4},
    {"name": "Technical Report", "format": "article", "pages": 0},
    {"name": "Thesis Chapter",   "format": "article", "pages": 0},
]

# format -> download info for LaTeX template files
# URLs may include {year} and {prev_year} placeholders (auto-filled at runtime)
TEMPLATE_SOURCES = {
    "neurips": {
        "urls": [
            "https://media.neurips.cc/Conferences/NeurIPS{year}/Styles/neurips_{year}.zip",
            "https://media.neurips.cc/Conferences/NeurIPS{prev_year}/Styles/neurips_{prev_year}.zip",
        ],
        # key style file that must be present for a successful download
        "required": ["neurips_{year}.sty", "neurips_{prev_year}.sty"],
    },
    "icml": {
        "urls": [
            "https://media.icml.cc/Conferences/ICML{year}/Styles/icml{year}.zip",
            "https://media.icml.cc/Conferences/ICML{prev_year}/Styles/icml{prev_year}.zip",
        ],
        "required": ["icml{year}.sty", "icml{prev_year}.sty"],
    },
    "iclr": {
        # GitHub master branch — year-agnostic, always current
        "urls": ["https://github.com/ICLR/Master-Template/archive/refs/heads/master.zip"],
        "required": ["iclr"],  # prefix match
    },
    "acl": {
        "urls": ["https://github.com/acl-org/acl-style-files/archive/refs/heads/master.zip"],
        "required": ["acl"],
    },
    "cvpr": {
        "urls": ["https://github.com/cvpr-org/author-kit/archive/refs/heads/main.zip"],
        "required": ["cvpr"],
    },
    "ieee": {
        "urls": ["https://mirrors.ctan.org/macros/latex/contrib/IEEEtran.zip"],
        "in_texlive": True,
        "required": ["IEEEtran.cls"],
    },
    "acm": {"urls": [], "in_texlive": True, "required": ["acmart.cls"]},
    "sigplan": {"urls": [], "in_texlive": True, "required": ["acmart.cls"]},
    "usenix": {
        # Direct .sty download (not a zip) — handled by _download_template
        "urls": ["https://www.usenix.org/sites/default/files/usenix-2020-09.sty"],
        "required": ["usenix"],
    },
    "mlsys": {
        "urls": [
            "https://media.mlsys.org/Conferences/MLSYS{year}/mlsys{year}style.zip",
            "https://media.mlsys.org/Conferences/MLSYS{prev_year}/mlsys{prev_year}style.zip",
        ],
        "required": ["mlsys"],
    },
    "article": {"urls": [], "in_texlive": True, "required": []},
}


# ============================================================
#  Terminal helpers
# ============================================================

from ark.ui import Style, Icons, styled, score_sparkline, score_trend, strip_ansi


class Colors:
    """Backward-compatible alias for ark.ui.Style."""
    BOLD   = Style.BOLD
    GREEN  = Style.GREEN
    YELLOW = Style.YELLOW
    RED    = Style.RED
    CYAN   = Style.CYAN
    DIM    = Style.DIM
    RESET  = Style.RESET


def _isatty() -> bool:
    return hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()


def _c(text, color):
    if not _isatty():
        return str(text)
    return f"{color}{text}{Style.RESET}"


def prompt_input(label: str, default: str = "") -> str:
    """Prompt for input with optional default."""
    if default:
        raw = input(f"{label} ({_c(default, Colors.DIM)}): ").strip()
        return raw if raw else default
    return input(f"{label}: ").strip()


def prompt_yn(label: str, default: bool = True) -> bool:
    """Prompt for yes/no."""
    hint = "[y/n]"
    d = "(y)" if default else "(n)"
    raw = input(f"{label} {_c(hint, Colors.BOLD)} {_c(d, Colors.DIM)}: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def prompt_choice(label: str, options: list, default: int = 1) -> int:
    """Prompt user to select from numbered options. Returns 0-based index."""
    raw = input(f"{label} ({default}): ").strip()
    if not raw:
        return default - 1
    try:
        idx = int(raw)
        if 1 <= idx <= len(options):
            return idx - 1
    except ValueError:
        pass
    print(f"  Invalid choice, using default ({default})")
    return default - 1


def _analyze_research_idea(idea: str) -> dict:
    """Analyze research idea using Claude Haiku and return summary + suggested title."""
    prompt = (
        "You are a research advisor. Given the following research idea, produce:\n"
        "1. A concise one-paragraph summary of what the research aims to achieve.\n"
        "2. A suggested academic paper title (concise, informative).\n\n"
        "Research idea:\n" + idea + "\n\n"
        "Respond in EXACTLY this format (no extra text):\n"
        "SUMMARY: <one paragraph summary>\n"
        "TITLE: <suggested paper title>"
    )
    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "--print", "--model", _get_configured_model(), "-p", prompt],
            capture_output=True, text=True, timeout=60, env=env,
        )
        response = result.stdout.strip()
        if not response:
            return {"summary": "", "suggested_title": ""}
        summary = ""
        suggested_title = ""
        for line in response.split("\n"):
            line = line.strip()
            if line.upper().startswith("SUMMARY:"):
                summary = line[len("SUMMARY:"):].strip()
            elif line.upper().startswith("TITLE:"):
                suggested_title = line[len("TITLE:"):].strip()
        return {"summary": summary, "suggested_title": suggested_title}
    except Exception:
        return {"summary": "", "suggested_title": ""}


# ============================================================
#  LaTeX template helpers
# ============================================================

def _resolve_template_urls(venue_format: str) -> tuple[list[str], list[str]]:
    """Return (urls, required_prefixes) with {year}/{prev_year} substituted."""
    import datetime
    year = datetime.datetime.now().year
    prev_year = year - 1

    source = TEMPLATE_SOURCES.get(venue_format, {})
    raw_urls = source.get("urls", [])
    raw_required = source.get("required", [])

    def sub(s):
        return s.replace("{year}", str(year)).replace("{prev_year}", str(prev_year))

    urls = [sub(u) for u in raw_urls]
    required = [sub(r) for r in raw_required]
    return urls, required


def _validate_template_files(latex_path: Path, required: list[str]) -> bool:
    """Check that at least one required style file exists (prefix match)."""
    if not required:
        return True  # nothing to check
    existing = [f.name for f in latex_path.iterdir() if f.suffix in (".sty", ".cls")]
    for req in required:
        if any(req in name for name in existing):
            return True
    return False


def _download_template(venue_format: str, latex_path: Path) -> bool:
    """Try downloading venue template files. Returns True if style files found.

    For unknown formats not in TEMPLATE_SOURCES, automatically tries CTAN
    before falling back to asking the user.
    """
    import tempfile
    import urllib.request
    import zipfile

    urls, required = _resolve_template_urls(venue_format)

    # For unknown formats: auto-search CTAN before giving up
    if not urls and venue_format not in TEMPLATE_SOURCES:
        ctan_url = f"https://mirrors.ctan.org/macros/latex/contrib/{venue_format}.zip"
        print(f"  {_c(f'Unknown format — trying CTAN: {ctan_url}', Colors.DIM)}")
        urls = [ctan_url]
        required = [venue_format]

    if not urls:
        return False

    tmp_path = None
    for url in urls:
        try:
            print(f"  {_c('Downloading template...', Colors.DIM)} {url[:70]}...")
            url_suffix = Path(url.split("?")[0]).suffix.lower()

            # Direct .sty / .cls file (not a zip)
            if url_suffix in (".sty", ".cls", ".bst"):
                fname = Path(url.split("?")[0]).name
                dest = latex_path / fname
                urllib.request.urlretrieve(url, dest)
                if dest.exists() and dest.stat().st_size > 100:
                    print(f"  {_c('Downloaded:', Colors.GREEN)} {fname}")
                    if _validate_template_files(latex_path, required):
                        return True
                continue

            # Zip archive
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_path = tmp.name
            urllib.request.urlretrieve(url, tmp_path)

            extracted = []
            with zipfile.ZipFile(tmp_path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = Path(info.filename).name
                    if Path(name).suffix.lower() in (".sty", ".cls", ".bst", ".bib"):
                        dest = latex_path / name
                        if not dest.exists():
                            with zf.open(info) as src, open(dest, "wb") as dst:
                                dst.write(src.read())
                            extracted.append(name)

            os.unlink(tmp_path)
            tmp_path = None

            if extracted and _validate_template_files(latex_path, required):
                print(f"  {_c('Downloaded:', Colors.GREEN)} {', '.join(extracted[:6])}")
                return True
            elif extracted:
                print(f"  {_c('Downloaded files but required style not found, trying next URL...', Colors.YELLOW)}")
            else:
                print(f"  {_c('No style files in archive, trying next URL...', Colors.YELLOW)}")
        except Exception as e:
            print(f"  {_c(f'Download failed: {e}', Colors.YELLOW)}")
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                tmp_path = None

    return False


def _detect_package_name(venue_format: str, latex_path: Path) -> dict[str, str]:
    """Scan downloaded .sty/.cls files and return actual package names for this venue.

    Returns a dict of placeholder -> actual name, e.g.:
      {"neurips_YEAR": "neurips_2026", "iclr_YEAR_conference": "iclr2026_conference"}
    """
    if not latex_path.exists():
        return {}

    sty_files = [f.stem for f in latex_path.iterdir() if f.suffix in (".sty", ".cls")]
    result = {}

    if venue_format == "neurips":
        for s in sty_files:
            if s.startswith("neurips_"):
                result["neurips_YEAR"] = s
                break
    elif venue_format == "icml":
        for s in sty_files:
            if s.startswith("icml"):
                result["icml_YEAR"] = s
                break
    elif venue_format == "iclr":
        for s in sty_files:
            if s.startswith("iclr") and "conference" in s:
                result["iclr_YEAR_conference"] = s
                break
    elif venue_format == "mlsys":
        for s in sty_files:
            if s.startswith("mlsys"):
                result["mlsys_YEAR"] = s
                break
    elif venue_format == "usenix":
        for s in sty_files:
            if "usenix" in s:
                result["usenix_YEAR"] = s
                break

    return result


def _get_main_tex_content(venue_format: str, title: str, venue_name: str, authors: list,
                          latex_path: Path = None) -> str:
    """Generate main.tex content based on venue_format. If latex_path is provided, infer package names from downloaded files."""
    author_str = " \\and ".join(authors) if authors else "Author Name"
    title = title or "Paper Title"

    # Detect actual package names from downloaded files
    pkg = _detect_package_name(venue_format, latex_path) if latex_path else {}

    sections = r"""
\section{Introduction}



\section{Related Work}



\section{Method}



\section{Experiments}



\section{Conclusion}



\bibliographystyle{plainnat}
\bibliography{references}

"""

    if venue_format == "neurips":
        neurips_pkg = pkg.get("neurips_YEAR", "neurips_2025")
        return rf"""\documentclass{{article}}
\usepackage{{{neurips_pkg}}}
\usepackage[utf8]{{inputenc}}
\usepackage{{amsmath,amssymb,amsfonts}}
\usepackage{{graphicx}}
\usepackage{{booktabs}}
\usepackage{{hyperref}}

\title{{{title}}}
\author{{{author_str}}}

\begin{{document}}
\maketitle

\begin{{abstract}}
TODO: Write abstract.
\end{{abstract}}
{sections}
\end{{document}}
"""

    if venue_format == "icml":
        icml_pkg = pkg.get("icml_YEAR", "icml2026")
        return rf"""\documentclass[accepted]{{article}}
\usepackage{{{icml_pkg}}}
\usepackage[utf8]{{inputenc}}
\usepackage{{amsmath,amssymb}}
\usepackage{{graphicx}}
\usepackage{{booktabs}}
\usepackage{{hyperref}}

\icmltitlerunning{{{title}}}  % running title

\begin{{document}}
\twocolumn[
\icmltitle{{{title}}}
\icmlsetsymbol{{equal}}{{\footnotemark[1]}}
\begin{{icmlauthorlist}}
\icmlauthor{{{author_str}}}{{}}
\end{{icmlauthorlist}}
]
\printAffiliationsAndNotice{{}}

\begin{{abstract}}
TODO: Write abstract.
\end{{abstract}}
{sections}
\end{{document}}
"""

    if venue_format == "iclr":
        iclr_pkg = pkg.get("iclr_YEAR_conference", "iclr2026_conference")
        return rf"""\documentclass{{article}}
\usepackage{{{iclr_pkg}}}
\usepackage[utf8]{{inputenc}}
\usepackage{{amsmath,amssymb}}
\usepackage{{graphicx}}
\usepackage{{booktabs}}
\usepackage{{hyperref}}

\title{{{title}}}
\author{{{author_str}}}

\begin{{document}}
\maketitle

\begin{{abstract}}
TODO: Write abstract.
\end{{abstract}}
{sections}
\end{{document}}
"""

    if venue_format == "acl":
        return rf"""\documentclass[11pt]{{article}}
\usepackage{{acl}}
\usepackage[utf8]{{inputenc}}
\usepackage{{amsmath,amssymb}}
\usepackage{{graphicx}}
\usepackage{{booktabs}}

\title{{{title}}}
\author{{{author_str}}}

\begin{{document}}
\maketitle

\begin{{abstract}}
TODO: Write abstract.
\end{{abstract}}
{sections}
\end{{document}}
"""

    if venue_format == "cvpr":
        return rf"""\documentclass[10pt,twocolumn,letterpaper]{{article}}
\usepackage{{cvpr}}
\usepackage[utf8]{{inputenc}}
\usepackage{{amsmath,amssymb}}
\usepackage{{graphicx}}
\usepackage{{booktabs}}

\title{{{title}}}
\author{{{author_str}}}

\begin{{document}}
\maketitle

\begin{{abstract}}
TODO: Write abstract.
\end{{abstract}}
{sections}
\end{{document}}
"""

    if venue_format == "ieee":
        return rf"""\documentclass[conference]{{IEEEtran}}
\usepackage[utf8]{{inputenc}}
\usepackage{{amsmath,amssymb}}
\usepackage{{graphicx}}
\usepackage{{booktabs}}
\usepackage{{cite}}

\title{{{title}}}
\author{{{author_str}}}

\begin{{document}}
\maketitle

\begin{{abstract}}
TODO: Write abstract.
\end{{abstract}}
{sections}
\end{{document}}
"""

    if venue_format in ("acm", "sigplan"):
        acm_format = "sigplan" if venue_format == "sigplan" else "acmsmall"
        return rf"""\documentclass[{acm_format}]{{acmart}}
\usepackage[utf8]{{inputenc}}
\usepackage{{amsmath,amssymb}}
\usepackage{{graphicx}}
\usepackage{{booktabs}}

\title{{{title}}}
\author{{{author_str}}}

\begin{{document}}
\maketitle

\begin{{abstract}}
TODO: Write abstract.
\end{{abstract}}
{sections}
\end{{document}}
"""

    if venue_format == "usenix":
        usenix_pkg = pkg.get("usenix_YEAR", "usenix-2020-09")
        return rf"""\documentclass[letterpaper,twocolumn,10pt]{{article}}
\usepackage{{{usenix_pkg}}}
\usepackage[utf8]{{inputenc}}
\usepackage{{amsmath,amssymb}}
\usepackage{{graphicx}}
\usepackage{{booktabs}}

\title{{{title}}}
\author{{{author_str}}}

\begin{{document}}
\maketitle

\begin{{abstract}}
TODO: Write abstract.
\end{{abstract}}
{sections}
\end{{document}}
"""

    if venue_format == "mlsys":
        mlsys_pkg = pkg.get("mlsys_YEAR", "mlsys2025")
        return rf"""\documentclass{{article}}
\usepackage{{{mlsys_pkg}}}
\usepackage[utf8]{{inputenc}}
\usepackage{{amsmath,amssymb}}
\usepackage{{graphicx}}
\usepackage{{booktabs}}
\usepackage{{hyperref}}

\title{{{title}}}
\author{{{author_str}}}

\begin{{document}}
\maketitle

\begin{{abstract}}
TODO: Write abstract.
\end{{abstract}}
{sections}
\end{{document}}
"""

    # Default: article class (for course project, workshop, thesis, etc.)
    return rf"""\documentclass[11pt]{{article}}
\usepackage[utf8]{{inputenc}}
\usepackage{{amsmath,amssymb,amsfonts}}
\usepackage{{graphicx}}
\usepackage{{booktabs}}
\usepackage{{hyperref}}
\usepackage[margin=1in]{{geometry}}

\title{{{title}}}
\author{{{author_str}}}
\date{{\today}}

\begin{{document}}
\maketitle

\begin{{abstract}}
TODO: Write abstract.
\end{{abstract}}
{sections}
\end{{document}}
"""


def _telegram_ask_for_template(venue_format: str, latex_path: Path, config: dict) -> bool:
    """Send Telegram message asking user for template URL. Returns True if resolved."""
    from ark.telegram import TelegramConfig, send_and_wait
    import urllib.request

    tg_config = TelegramConfig.from_project_config(config)
    if not tg_config.is_configured:
        return False

    msg = (
        f"⚠️ *LaTeX template download failed*\n\n"
        f"Could not automatically download style files for `{venue_format}`.\n"
        f"Please provide a download URL for the template zip, or place `.sty`/`.cls` files in:\n"
        f"`{latex_path}/`\n\n"
        f"Reply with a URL to auto-download, or reply `ready` to skip."
    )

    print(f"  {_c('Waiting for template URL via Telegram (30 min timeout)...', Colors.DIM)}")
    _, required = _resolve_template_urls(venue_format)

    reply = send_and_wait(config, msg, timeout=1800)
    if not reply:
        return False

    if reply.lower() == "ready":
        if _validate_template_files(latex_path, required):
            return True
        return False
    elif reply.startswith("http"):
        print(f"  {_c(f'Trying user URL: {reply[:60]}', Colors.DIM)}...")
        try:
            import tempfile, zipfile
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_path = tmp.name
            urllib.request.urlretrieve(reply, tmp_path)
            extracted = []
            with zipfile.ZipFile(tmp_path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = Path(info.filename).name
                    if Path(name).suffix.lower() in (".sty", ".cls", ".bst"):
                        dest = latex_path / name
                        if not dest.exists():
                            with zf.open(info) as src, open(dest, "wb") as dst:
                                dst.write(src.read())
                            extracted.append(name)
            os.unlink(tmp_path)
            if extracted and _validate_template_files(latex_path, required):
                print(f"  {_c('Downloaded from user URL:', Colors.GREEN)} {', '.join(extracted[:4])}")
                return True
        except Exception as e:
            print(f"  {_c(f'Download failed: {e}', Colors.YELLOW)}")
    return False


def _telegram_confirm_before_research(config: dict) -> bool:
    """Send Telegram message with project summary and wait for user approval before deep research.

    Returns True if user approves (or Telegram not configured), False if user rejects.
    """
    from ark.telegram import TelegramConfig, send_and_wait

    tg_config = TelegramConfig.from_project_config(config)
    if not tg_config.is_configured:
        return True

    title = config.get("title", "Untitled")
    research_idea = config.get("research_idea", "")
    venue = config.get("venue", "")

    idea_preview = research_idea[:500] + ("..." if len(research_idea) > 500 else "") if research_idea else "(none)"
    msg = (
        f"📋 *Project Summary*\n\n"
        f"📰 *Title*: {title}\n"
        f"🎯 *Venue*: {venue}\n"
        f"📝 *Research Idea*:\n{idea_preview}\n\n"
        f"Reply *ok* to proceed with Deep Research, or send modifications."
    )

    print(f"  {_c('Waiting for Telegram confirmation (30 min timeout)...', Colors.DIM)}")
    reply = send_and_wait(config, msg, timeout=1800)

    if not reply:
        print(f"  {_c('No response, proceeding...', Colors.DIM)}")
        return True

    lower = reply.lower().strip()
    if lower in ("ok", "yes", "y", "proceed", "go"):
        print(f"  {_c('User approved via Telegram.', Colors.GREEN)}")
        return True
    elif lower in ("no", "n", "stop", "cancel", "abort"):
        print(f"  {_c('User cancelled via Telegram.', Colors.YELLOW)}")
        return False
    else:
        # Treat as modification
        if lower.startswith("title:"):
            new_title = reply[len("title:"):].strip()
            if new_title:
                config["title"] = new_title
                print(f"  {_c(f'Title updated: {new_title}', Colors.GREEN)}")
        elif lower.startswith("idea:"):
            new_idea = reply[len("idea:"):].strip()
            if new_idea:
                config["research_idea"] = new_idea
                print(f"  {_c('Research idea updated via Telegram.', Colors.GREEN)}")
        return True  # proceed after modification

    print(f"  {_c('Telegram confirmation timed out, proceeding anyway.', Colors.YELLOW)}")
    return True


def _setup_latex_template(code_dir: str, config: dict):
    """Create LaTeX template files under code_dir's latex_dir.

    1. If main.tex already exists, skip
    2. Try downloading venue template files (sty/cls/bst), URLs auto-adapt to current year
    3. Download failed -> if Telegram is configured, ask user to provide URL
    4. Still failed -> fall back to article base template (guaranteed to compile)
    5. Generate main.tex skeleton + references.bib
    """
    latex_dir = config.get("latex_dir", "paper")
    figures_dir = config.get("figures_dir", "paper/figures")
    latex_path = Path(code_dir) / latex_dir
    figures_path = Path(code_dir) / figures_dir

    main_tex = latex_path / "main.tex"
    if main_tex.exists():
        print(f"  {_c('LaTeX:', Colors.DIM)} main.tex already exists, skipping template setup.")
        return

    latex_path.mkdir(parents=True, exist_ok=True)
    figures_path.mkdir(parents=True, exist_ok=True)

    venue_format = config.get("venue_format", "article")
    venue_name = config.get("venue", "")
    title = config.get("title", "")
    authors = config.get("authors", [])

    print()
    print(f"{_c('LaTeX Template Setup', Colors.BOLD)}")

    # Try bundled venue_templates/ first (same as webapp)
    from website.dashboard.templates import has_venue_template, copy_venue_template
    downloaded = False

    _, required = _resolve_template_urls(venue_format)
    if _validate_template_files(latex_path, required):
        print(f"  {_c('Template files already present, skipping.', Colors.DIM)}")
        downloaded = True
    elif has_venue_template(venue_format):
        copy_venue_template(venue_format, latex_path)
        print(f"  {_c('Copied bundled template:', Colors.GREEN)} venue_templates/{venue_format}/")
        downloaded = True
    else:
        source = TEMPLATE_SOURCES.get(venue_format, {})
        if source.get("in_texlive") and not source.get("urls"):
            print(f"  {_c('Note:', Colors.DIM)} {venue_format} uses TeX Live built-in class.")
            downloaded = True
        else:
            # Fallback: try downloading from the internet
            downloaded = _download_template(venue_format, latex_path)
            if not downloaded:
                downloaded = _telegram_ask_for_template(venue_format, latex_path, config)
                if not downloaded:
                    print(f"  {_c('Using article fallback template (replace style files later).', Colors.YELLOW)}")
                    (latex_path / "TEMPLATE_MISSING.txt").write_text(
                        f"Auto-download of {venue_format} template failed.\n"
                        f"Please place the venue's .sty/.cls files in this directory.\n"
                        f"Then delete this file.\n"
                    )
                    venue_format = "article"

    # Generate main.tex: prefer committed venue_templates/<format>/main.tex if available
    venue_template_src = Path(__file__).parent.parent / "venue_templates" / venue_format / "main.tex"
    if venue_template_src.exists():
        content = venue_template_src.read_text()
        # Substitute title/author placeholders
        if title:
            content = content.replace("Paper Title", title, 1)
        if authors:
            author_str = " \\and ".join(authors)
            content = content.replace("Author Name", author_str, 1)
        print(f"  {_c('Using committed template:', Colors.DIM)} venue_templates/{venue_format}/main.tex")
    else:
        content = _get_main_tex_content(venue_format, title, venue_name, authors, latex_path)
    main_tex.write_text(content)
    print(f"  {_c('Created:', Colors.GREEN)} {main_tex}")

    # Create references.bib
    bib_file = latex_path / "references.bib"
    if not bib_file.exists():
        bib_file.write_text("% Add your BibTeX references here\n")
        print(f"  {_c('Created:', Colors.GREEN)} {bib_file}")

    print()


# ============================================================
#  PDF Spec Extraction (for ark new --from-pdf)
# ============================================================

def _extract_spec_from_pdf(pdf_path: str, instructions: str = "") -> dict:
    """Extract research spec from a PDF file using PyMuPDF + Claude Haiku."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print(f"  {_c('Error: PyMuPDF not installed. Run: pip install pymupdf', Colors.RED)}")
        return {}

    try:
        doc = fitz.open(pdf_path)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
    except Exception as e:
        print(f"  {_c(f'Error reading PDF: {e}', Colors.RED)}")
        return {}

    if len(text.strip()) < 100:
        print(f"  {_c('Warning: PDF has very little text content.', Colors.YELLOW)}")
        return {"raw_text": text}

    # Use Claude Haiku for structured analysis
    prompt = (
        "You are analyzing a research project proposal PDF. Extract the following:\n\n"
        "1. TITLE: The project/paper title\n"
        "2. AUTHORS: Comma-separated list of author names\n"
        "3. SUMMARY: One-paragraph summary of the research\n"
        "4. METHODOLOGY: Proposed methodology\n"
        "5. CODING_TASKS: If this involves building software, list the main coding tasks\n\n"
        "Respond in EXACTLY this format:\n"
        "TITLE: ...\n"
        "AUTHORS: Name1, Name2\n"
        "SUMMARY: ...\n"
        "METHODOLOGY: ...\n"
        "CODING_TASKS:\n- task1\n- task2\n"
        f"\nDocument text:\n{text[:8000]}"
    )
    if instructions:
        prompt += (
            "\n\nAdditional user instructions and caveats "
            "(follow these while extracting):\n"
            f"{instructions}"
        )

    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "--print", "--model", _get_configured_model(), "-p", prompt],
            capture_output=True, text=True, timeout=90, env=env,
        )
        response = result.stdout.strip()
        if response:
            spec = _parse_spec_analysis(response, text)
            if spec.get("title"):
                return spec
        # AI returned nothing useful, fall through to heuristic
        print(f"  {_c('AI analysis returned no results, using text extraction...', Colors.DIM)}")
    except Exception as e:
        print(f"  {_c(f'AI analysis failed, using text extraction...', Colors.DIM)}")

    # Fallback: extract title and authors from raw text heuristically
    return _extract_spec_heuristic(text)


def _extract_spec_heuristic(text: str) -> dict:
    """Extract title, authors, and summary from PDF text using heuristics."""
    spec = {"raw_text": text}
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return spec

    # Title: first non-empty line(s) before "Abstract" or author/affiliation lines
    title_lines = []
    pre_abstract_lines = []  # lines between title and abstract
    found_abstract = False
    abstract_text = ""

    AFFILIATION_KW = ["university", "institute", "kaust", "mit", "stanford", "berkeley",
                      "google", "microsoft", "meta", "dept", "department", "school of",
                      "college", "lab", "research"]

    collecting_title = True
    for i, line in enumerate(lines):
        lower = line.lower()
        if lower.startswith("abstract") or lower == "abstract":
            found_abstract = True
            collecting_title = False
            continue
        if found_abstract:
            if re.match(r"^\d+\s", line) or lower.startswith("introduction") or lower.startswith("1 "):
                break
            abstract_text += " " + line
            continue
        # Detect author/affiliation markers
        is_affiliation = any(kw in lower for kw in AFFILIATION_KW)
        has_author_symbol = "∗" in line or "†" in line or "‡" in line
        if collecting_title and not is_affiliation and not has_author_symbol and i < 5:
            title_lines.append(line)
        else:
            collecting_title = False
            if i < 15:
                pre_abstract_lines.append(line)

    if title_lines:
        spec["title"] = " ".join(title_lines)

    # Extract authors from pre-abstract lines (between title and abstract)
    if pre_abstract_lines:
        authors = []
        for line in pre_abstract_lines:
            lower = line.lower()
            # Skip pure affiliation lines
            if any(kw in lower for kw in AFFILIATION_KW):
                continue
            # Skip very short lines (e.g., email, symbols)
            if len(line.strip()) < 3:
                continue
            # Clean symbols and check if it looks like a name
            cleaned = re.sub(r'[∗†‡§¶\d,]', '', line).strip()
            if cleaned and len(cleaned.split()) <= 5 and cleaned[0].isupper():
                authors.append(cleaned)
        if authors:
            spec["authors"] = authors

    if abstract_text.strip():
        spec["summary"] = abstract_text.strip()

    return spec


def _parse_spec_analysis(response: str, raw_text: str) -> dict:
    """Parse the structured analysis response from Claude Haiku."""
    spec = {"raw_text": raw_text}
    current_key = None
    current_list = []

    for line in response.split("\n"):
        line_stripped = line.strip()
        if line_stripped.startswith("TITLE:"):
            spec["title"] = line_stripped[6:].strip()
            current_key = None
        elif line_stripped.startswith("AUTHORS:"):
            authors_str = line_stripped[8:].strip()
            if authors_str:
                spec["authors"] = [a.strip() for a in authors_str.split(",") if a.strip()]
            current_key = None
        elif line_stripped.startswith("SUMMARY:"):
            spec["summary"] = line_stripped[8:].strip()
            current_key = None
        elif line_stripped.startswith("METHODOLOGY:"):
            spec["methodology"] = line_stripped[12:].strip()
            current_key = None
        elif line_stripped.startswith("CODING_TASKS:"):
            if current_key and current_list:
                spec[current_key] = current_list
            current_key = "coding_tasks"
            current_list = []
        elif line_stripped.startswith("- ") and current_key:
            current_list.append(line_stripped[2:])

    if current_key and current_list:
        spec[current_key] = current_list

    return spec


# ============================================================
#  ark new / ark create
# ============================================================

def cmd_new(args):
    name = args.project
    projects_dir = get_projects_dir()
    project_dir = projects_dir / name

    if project_dir.exists():
        print(f"{_c('Error:', Colors.RED)} Project '{name}' already exists at {project_dir}")
        sys.exit(1)

    print()
    print(f"  {_c(f'Creating project: {name}', Colors.BOLD + Colors.CYAN)}")
    print()

    # ── PDF pre-load from --from-pdf flag (optional shortcut) ─
    pdf_spec = {}
    if hasattr(args, 'from_pdf') and args.from_pdf:
        pdf_path = os.path.expanduser(args.from_pdf)
        if not os.path.isfile(pdf_path):
            print(f"{_c('Error:', Colors.RED)} PDF not found: {pdf_path}")
            sys.exit(1)
        pdf_spec = _load_and_show_pdf(pdf_path)

    # ── Interactive wizard (PDF values used as defaults) ──────
    return _cmd_new_wizard(args, name, project_dir, pdf_spec)


def _load_and_show_pdf(pdf_path: str, instructions: str = "") -> dict:
    """Load PDF and show basic info. Deep analysis is done by the initializer agent during Research Phase."""
    print(f"  {_c('PDF loaded:', Colors.GREEN)} {os.path.basename(pdf_path)}")
    print(f"  {_c('Note:', Colors.DIM)} Full analysis will run during Research Phase (initializer agent reads PDF directly)")
    print()
    return {"pdf_path": os.path.abspath(pdf_path)}


def _wizard_step_header(step_num: int, title: str):
    """Print a decorated wizard step header."""
    from ark.ui import Icons
    icon = Icons.for_wizard_step(step_num)
    label = f" {icon}  Step {step_num}: {title} "
    width = 52
    pad = max(0, width - len(label) - 2)
    print(f"  {_c(f'┌─{label}' + '─' * pad + '┐', Colors.CYAN)}")


def _wizard_step_footer():
    """Print wizard step footer."""
    print(f"  {_c('└' + '─' * 52 + '┘', Colors.DIM)}")
    print()


def _setup_project_telegram(project_name: str = "") -> dict:
    """Interactive per-project Telegram setup. Returns dict with
    telegram_bot_token + telegram_chat_id, or {} if skipped."""
    import json as _json
    import urllib.request as _ur
    import time as _time

    print(f"  Get your Bot Token from @BotFather in Telegram (/newbot).")
    tg_token = prompt_input("  Bot Token (Enter to skip)").strip()
    if not tg_token:
        return {}

    # Auto-detect chat_id via getUpdates (3 retries)
    print()
    print(f"  {_c('Now go to Telegram and send any message to your bot.', Colors.BOLD)}")
    input("  Press Enter when done... ")

    tg_chat_id = None
    for attempt in range(3):
        try:
            url = f"https://api.telegram.org/bot{tg_token}/getUpdates"
            with _ur.urlopen(url, timeout=10) as resp:
                data = _json.loads(resp.read())
            results = data.get("result", [])
            if results:
                last = results[-1]
                msg = last.get("message") or last.get("edited_message") or {}
                sender = msg.get("from", {})
                tg_chat_id = str(sender.get("id") or msg.get("chat", {}).get("id", ""))
                if tg_chat_id:
                    print(f"  {_c(f'→ Chat ID detected: {tg_chat_id}', Colors.GREEN)}")
                    break
        except Exception as e:
            print(f"  {_c(f'Warning: {e}', Colors.YELLOW)}")
        if attempt < 2:
            print(f"  No messages found yet, retrying in 3s...")
            _time.sleep(3)

    if not tg_chat_id:
        print(f"  {_c('Could not auto-detect Chat ID.', Colors.YELLOW)}")
        tg_chat_id = prompt_input("  Enter Chat ID manually").strip()

    if not tg_chat_id:
        return {}

    # Send test message
    try:
        url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
        data = _json.dumps({
            "chat_id": tg_chat_id,
            "text": f"✅ ARK Bot configured for project '{project_name}'!",
        }).encode("utf-8")
        req = _ur.Request(url, data=data, headers={"Content-Type": "application/json"})
        _ur.urlopen(req, timeout=10)
        print(f"  {_c('→ Test message sent! Check your Telegram.', Colors.GREEN)}")
    except Exception as e:
        print(f"  {_c(f'Warning: test message failed: {e}', Colors.YELLOW)}")

    return {
        "telegram_bot_token": tg_token,
        "telegram_chat_id": tg_chat_id,
    }


def _cmd_new_wizard(args, name: str, project_dir: Path, pdf_spec: dict):
    """Interactive wizard for project creation. PDF values used as defaults."""
    from ark.ui import Icons

    # ── Step 1: Code Directory ────────────────────────────────
    _wizard_step_header(1, "Code Directory")
    print("  Where is your research code located?")
    default_code_dir = str((Path.cwd() / "projects" / name).resolve())
    code_dir = prompt_input("Code directory", default_code_dir)
    code_dir = os.path.expanduser(code_dir)

    if not os.path.isdir(code_dir):
        print(f"  {_c(f'Warning: Directory does not exist: {code_dir}', Colors.YELLOW)}")
        if prompt_yn("  Create it?", default=True):
            os.makedirs(code_dir, exist_ok=True)
        else:
            print("  Aborted.")
            sys.exit(1)
    print(f"  {_c('✓', Colors.GREEN)} {code_dir}")
    _wizard_step_footer()

    # ── Step 2: Target Venue ──────────────────────────────────
    _wizard_step_header(2, "Target Venue")
    print(f"  {_c('Conferences:', Colors.DIM)}")
    for i, v in enumerate(VENUES, 1):
        pages_str = _c(str(v['pages']) + " pages", Colors.BOLD)
        print(f"  {_c(f'{i:>2}.', Colors.BOLD)} {v['name']:<14} ({v['format']}, {pages_str})")
    base = len(VENUES)
    print(f"  {_c('Other:', Colors.DIM)}")
    for i, v in enumerate(OTHER_VENUES, base + 1):
        pages_str = _c("no limit", Colors.BOLD) if v['pages'] == 0 else _c(str(v['pages']) + " pages", Colors.BOLD)
        print(f"  {_c(f'{i:>2}.', Colors.BOLD)} {v['name']:<18} ({v['format']}, {pages_str})")
    custom_idx = base + len(OTHER_VENUES) + 1
    print(f"  {_c(f'{custom_idx:>2}.', Colors.BOLD)} Custom")

    all_options = VENUES + OTHER_VENUES + [{"name": "Custom"}]
    venue_idx = prompt_choice("Select venue", all_options, default=1)

    if venue_idx < len(VENUES):
        venue = VENUES[venue_idx]
        venue_name = venue["name"]
        venue_format = venue["format"]
        venue_pages = venue["pages"]
    elif venue_idx < len(VENUES) + len(OTHER_VENUES):
        venue = OTHER_VENUES[venue_idx - len(VENUES)]
        venue_name = venue["name"]
        venue_format = venue["format"]
        venue_pages = venue["pages"]
    else:
        venue_name = prompt_input("Venue name")
        venue_format = prompt_input("LaTeX format (e.g., neurips, acl, ieee, article)")
        try:
            venue_pages = int(prompt_input("Page limit (0=no limit)"))
        except ValueError:
            venue_pages = 0

    # Allow page limit override
    default_pages = str(venue_pages) if venue_pages > 0 else "0"
    pages_raw = prompt_input("Page limit (0=no limit)", default_pages)
    try:
        venue_pages = int(pages_raw)
    except ValueError:
        pass

    pages_display = f"{venue_pages} pages" if venue_pages > 0 else "no limit"
    print(f"  {_c('✓', Colors.GREEN)} {venue_name} ({venue_format}, {pages_display})")
    _wizard_step_footer()

    # ── Step 3: Research Idea ─────────────────────────────
    _wizard_step_header(3, "Research Idea")

    pdf_instructions = ""

    # If no PDF loaded yet via --from-pdf, offer a choice
    if not pdf_spec:
        print(f"  How would you like to provide the research idea?")
        print(f"  {_c('1.', Colors.BOLD)} Enter manually")
        print(f"  {_c('2.', Colors.BOLD)} Read from PDF (idea / proposal / draft)")
        idea_choice = prompt_input("  Choice", "1").strip()

        if idea_choice == "2":
            pdf_path = prompt_input("  PDF path").strip()
            pdf_path = os.path.expanduser(pdf_path)
            if os.path.isfile(pdf_path):
                pdf_instructions = prompt_input("  Instructions / notes (optional)").strip()
                pdf_spec = _load_and_show_pdf(pdf_path, instructions=pdf_instructions)
            else:
                print(f"  {_c('File not found, falling back to manual input.', Colors.YELLOW)}")
        print()

    pdf_summary = pdf_spec.get("summary", "")
    if pdf_summary:
        print(f"  {_c('From PDF:', Colors.DIM)} {pdf_summary[:150]}...")
        research_idea = pdf_summary
        if not prompt_yn("  Use this as research idea?", default=True):
            print("  Enter research idea (multi-line, empty line to finish):")
            idea_lines = []
            while True:
                line = input("  > ").rstrip()
                if not line:
                    break
                idea_lines.append(line)
            research_idea = "\n".join(idea_lines).strip()
    else:
        print("  Describe your research idea (multi-line, empty line to finish):")
        idea_lines = []
        while True:
            line = input("  > ").rstrip()
            if not line:
                break
            idea_lines.append(line)
        research_idea = "\n".join(idea_lines).strip()

    if not research_idea:
        print(f"  {_c('⚠ No research idea provided.', Colors.YELLOW)}")

    # AI analysis (skip if we already have PDF title)
    ai_analysis = {"summary": "", "suggested_title": ""}
    default_title = pdf_spec.get("title", "")
    if not default_title and research_idea:
        print()
        print(f"  {_c('Analyzing research idea...', Colors.DIM)}")
        ai_analysis = _analyze_research_idea(research_idea)
        if ai_analysis["suggested_title"]:
            default_title = ai_analysis["suggested_title"]
            print(f"  {_c('Suggested title:', Colors.BOLD)} {default_title}")
        print()

    # Title
    title = prompt_input("  Paper title", default_title)

    # Build goal_anchor from research idea
    goal_anchor = ""
    if title or research_idea:
        goal_anchor = f"## Goal Anchor\n\n"
        if title:
            goal_anchor += f"**Paper Title**: {title}\n"
        goal_anchor += f"**Target Venue**: {venue_name} ({venue_format}, {venue_pages} pages)\n\n"
        if research_idea:
            goal_anchor += f"**Research Idea**:\n{research_idea}\n"
    _wizard_step_footer()

    # ── Step 4: Authors (optional) ────────────────────────────
    _wizard_step_header(4, "Authors (optional)")
    authors = pdf_spec.get("authors", [])
    if authors:
        print(f"  {_c('From PDF:', Colors.DIM)} {', '.join(authors)}")
        if not prompt_yn("  Use these authors?", default=True):
            authors = []
    if not authors:
        if prompt_yn("Add authors now?", default=False):
            print("Enter authors one per line, empty line to finish:")
            while True:
                author = input("  Author: ").strip()
                if not author:
                    break
                authors.append(author)
    _wizard_step_footer()

    # ── Step 5: Experiment Compute ────────────────────────────
    _wizard_step_header(5, "Experiment Compute")
    print("  How do you run experiments?")
    compute_options = [
        ("Slurm HPC (MCNodes, etc.)", "slurm"),
        ("Local machine", "local"),
        ("Cloud (AWS/GCP/Azure)", "cloud"),
        ("Other / Custom", "custom"),
    ]
    for i, (display, _) in enumerate(compute_options, 1):
        print(f"  {_c(f'{i}.', Colors.BOLD)} {display}")
    compute_idx = prompt_choice("Select compute backend", compute_options, default=1)
    compute_type = compute_options[compute_idx][1]
    compute_backend = {"type": compute_type}

    if compute_type == "slurm":
        # Auto-discover cluster
        print(f"  {_c('Discovering cluster...', Colors.DIM)}")
        try:
            sinfo = subprocess.run(
                ["sinfo", "-o", "%P %G %c %m %a"],
                capture_output=True, text=True, timeout=15,
            )
            if sinfo.returncode == 0 and sinfo.stdout.strip():
                print(f"  {_c('Cluster partitions:', Colors.GREEN)}")
                for line in sinfo.stdout.strip().split("\n")[:8]:
                    print(f"    {line}")
        except Exception:
            print(f"  {_c('sinfo not available (not on login node?)', Colors.YELLOW)}")

        try:
            acct = subprocess.run(
                ["sacctmgr", "show", "assoc",
                 f"user={os.environ.get('USER', '')}", "format=Account", "-n"],
                capture_output=True, text=True, timeout=15,
            )
            if acct.returncode == 0 and acct.stdout.strip():
                accounts = [a.strip() for a in acct.stdout.strip().split("\n") if a.strip()]
                if accounts:
                    print(f"  {_c('Slurm accounts:', Colors.GREEN)} {', '.join(accounts[:5])}")
        except Exception:
            pass

        default_prefix = f"{name.upper()}_"
        compute_backend["job_prefix"] = prompt_input("  Job name prefix", default_prefix)
        compute_backend["conda_env"] = prompt_input("  Conda environment", name.lower())

        if prompt_yn("  Provide a template .slurm file?", default=False):
            template = prompt_input("  Path to .slurm template").strip()
            if template:
                compute_backend["slurm_template"] = template

    elif compute_type == "local":
        compute_backend["conda_env"] = prompt_input("  Conda environment", name.lower())
        gpu_raw = prompt_input("  Number of GPUs (0 = CPU only)", "0")
        try:
            compute_backend["gpu_count"] = int(gpu_raw)
        except ValueError:
            compute_backend["gpu_count"] = 0

    elif compute_type == "cloud":
        print("  Cloud providers:")
        providers = [("AWS (EC2)", "aws"), ("Google Cloud (GCE)", "gcp"), ("Azure (VM)", "azure")]
        for i, (display, _) in enumerate(providers, 1):
            print(f"    {_c(f'{i}.', Colors.BOLD)} {display}")
        prov_idx = prompt_choice("  Select provider", providers, default=1)
        provider = providers[prov_idx][1]
        compute_backend["provider"] = provider

        # Check CLI availability
        cli_tools = {"aws": "aws", "gcp": "gcloud", "azure": "az"}
        cli_name = cli_tools[provider]
        try:
            subprocess.run([cli_name, "--version"], capture_output=True, timeout=10)
            print(f"  {_c(f'{cli_name} CLI found', Colors.GREEN)}")
        except Exception:
            print(f"  {_c(f'Warning: {cli_name} CLI not found. Install it before running experiments.', Colors.YELLOW)}")

        if provider == "aws":
            compute_backend["region"] = prompt_input("  AWS region", "us-east-1")
            compute_backend["instance_type"] = prompt_input("  Instance type", "p3.2xlarge")
            compute_backend["image_id"] = prompt_input("  AMI ID (Deep Learning AMI recommended)")
            compute_backend["ssh_key_name"] = prompt_input("  SSH key pair name")
            compute_backend["ssh_key_path"] = prompt_input("  SSH private key path", "~/.ssh/id_rsa")
            sg = prompt_input("  Security group ID (Enter to skip)", "").strip()
            if sg:
                compute_backend["security_group"] = sg
        elif provider == "gcp":
            compute_backend["region"] = prompt_input("  GCP zone", "us-central1-a")
            compute_backend["instance_type"] = prompt_input("  Machine type", "n1-standard-8")
            compute_backend["image_id"] = prompt_input("  Image family", "pytorch-latest-gpu")
            compute_backend["ssh_key_path"] = prompt_input("  SSH private key path", "~/.ssh/id_rsa")
            accel = prompt_input("  Accelerator type (e.g. nvidia-tesla-v100, Enter to skip)", "").strip()
            if accel:
                compute_backend["accelerator_type"] = accel
                accel_count = prompt_input("  Accelerator count", "1")
                try:
                    compute_backend["accelerator_count"] = int(accel_count)
                except ValueError:
                    compute_backend["accelerator_count"] = 1
        elif provider == "azure":
            compute_backend["region"] = prompt_input("  Azure location", "eastus")
            compute_backend["instance_type"] = prompt_input("  VM size", "Standard_NC6s_v3")
            compute_backend["image_id"] = prompt_input("  Image URN", "Canonical:UbuntuServer:18.04-LTS:latest")
            compute_backend["ssh_key_path"] = prompt_input("  SSH private key path", "~/.ssh/id_rsa")
            rg = prompt_input("  Resource group (Enter for auto)", "").strip()
            if rg:
                compute_backend["resource_group"] = rg

        compute_backend["ssh_user"] = prompt_input("  SSH username", "ubuntu")
        compute_backend["conda_env"] = prompt_input("  Conda environment", name.lower())

        print("  Setup commands (run on instance after provisioning, empty line to finish):")
        setup_cmds = []
        while True:
            cmd_line = input("    > ").strip()
            if not cmd_line:
                break
            setup_cmds.append(cmd_line)
        if setup_cmds:
            compute_backend["setup_commands"] = setup_cmds

    elif compute_type == "custom":
        compute_backend["conda_env"] = prompt_input("  Conda environment", name.lower())
        print("  Enter custom compute instructions (multi-line, empty line to finish):")
        custom_lines = []
        while True:
            line = input("    > ").rstrip()
            if not line:
                break
            custom_lines.append(line)
        if custom_lines:
            compute_backend["instructions"] = "\n".join(custom_lines)

    print(f"  {_c('✓', Colors.GREEN)} Compute: {compute_type}")
    _wizard_step_footer()

    # ── Step 6: AI Model ──────────────────────────────────────
    _wizard_step_header(6, "AI Model")
    models = [
        ("Claude", "claude"),
        ("Gemini", "gemini"),
        ("Codex", "codex"),
    ]
    for i, (display, _) in enumerate(models, 1):
        rec = " (recommended)" if i == 1 else ""
        print(f"  {_c(f'{i}.', Colors.BOLD)} {display}{rec}")
    model_idx = prompt_choice("Select model", models, default=1)
    model = models[model_idx][1]
    print(f"  {_c('✓', Colors.GREEN)} Selected: {model}")
    _wizard_step_footer()

    # ── Step 7: Figure Generation ────────────────────────────
    _wizard_step_header(7, "Figure Generation")
    print("  How should concept figures (architecture, mechanism diagrams) be generated?")
    fig_options = [
        ("Nano Banana AI + Matplotlib", "nano_banana"),
        ("Matplotlib only", "matplotlib_only"),
    ]
    for i, (display, _) in enumerate(fig_options, 1):
        rec = " (recommended)" if i == 1 else ""
        desc = ""
        if i == 1:
            desc = f"\n    {_c('AI generates concept diagrams, matplotlib handles data plots', Colors.DIM)}"
        print(f"  {_c(f'{i}.', Colors.BOLD)} {display}{rec}{desc}")
    fig_idx = prompt_choice("Select figure method", fig_options, default=1)
    figure_generation = fig_options[fig_idx][1]
    print(f"  {_c('✓', Colors.GREEN)} Selected: {fig_options[fig_idx][0]}")

    nano_banana_model = "flash"
    if figure_generation == "nano_banana":
        # Check for Gemini API key
        from ark.deep_research import get_gemini_api_key
        if not get_gemini_api_key():
            print(f"  {_c('Note:', Colors.YELLOW)} No Gemini API key found in environment.")
            print(f"  Export it before running: export GEMINI_API_KEY=YOUR_KEY")
        else:
            print(f"  {_c('✓', Colors.GREEN)} Gemini API key found")

        # Model choice
        model_opts = [("Flash (fast, free tier)", "flash"), ("Pro (highest quality, $0.13/img)", "pro")]
        for i, (display, _) in enumerate(model_opts, 1):
            default_marker = " (default)" if i == 1 else ""
            print(f"    {_c(f'{i}.', Colors.BOLD)} {display}{default_marker}")
        raw_model = prompt_input("  Nano Banana model (1-2)", "1").strip()
        try:
            m_idx = int(raw_model) - 1
            if m_idx < 0 or m_idx >= len(model_opts):
                m_idx = 0
        except ValueError:
            m_idx = 0
        nano_banana_model = model_opts[m_idx][1]
        print(f"  {_c('✓', Colors.GREEN)} Model: {model_opts[m_idx][0]}")
    _wizard_step_footer()

    # ── Step 8: Language ─────────────────────────────────────
    _wizard_step_header(8, "Language")
    print("  Language for Telegram notifications and agent responses:")
    lang_options = [("English", "en"), ("Chinese (中文)", "zh")]
    for i, (display, _) in enumerate(lang_options, 1):
        default_marker = " (default)" if i == 1 else ""
        print(f"  {_c(f'{i}.', Colors.BOLD)} {display}{default_marker}")
    raw_lang = prompt_input("Select (1-2)", "1").strip()
    try:
        lang_idx = int(raw_lang) - 1
        if lang_idx < 0 or lang_idx >= len(lang_options):
            lang_idx = 0
    except ValueError:
        lang_idx = 0
    language = lang_options[lang_idx][1]
    print(f"  {_c('✓', Colors.GREEN)} Selected: {lang_options[lang_idx][0]}")
    _wizard_step_footer()

    # ── Step 9: Telegram Bot (per-project) ───────────────────
    _wizard_step_header(9, "Telegram Bot (per-project)")
    print("  Each project gets its own dedicated Telegram bot.")
    if prompt_yn("  Set up Telegram bot for this project now?", default=True):
        tg_config_dict = _setup_project_telegram(name)
    else:
        print("  Skipped. Run `ark setup-bot <project>` later.")
        tg_config_dict = {}
    _wizard_step_footer()

    # ── Build config ──────────────────────────────────────────
    config = {
        "code_dir": code_dir,
        "venue": venue_name,
        "venue_format": venue_format,
        "venue_pages": venue_pages,
        "title": title,
        "model": model,
        "compute_backend": compute_backend,
        "paper_accept_threshold": 8,
        "latex_dir": "paper",
        "figures_dir": "paper/figures",
        "scripts_dir": "code",
        "create_figures_script": "code/create_paper_figures.py",
        "figure_generation": figure_generation,
        "nano_banana_model": nano_banana_model,
        "language": language,
    }
    if authors:
        config["authors"] = authors
    if research_idea:
        config["research_idea"] = research_idea
    if goal_anchor:
        config["goal_anchor"] = goal_anchor

    # Dev mode config from PDF spec
    if pdf_spec.get("coding_tasks"):
        config["mode"] = "dev"
        config["test_command"] = "pytest -v"
        config["code_review_threshold"] = 7
    if hasattr(args, 'from_pdf') and args.from_pdf:
        config["spec_pdf"] = os.path.abspath(args.from_pdf)
    if pdf_instructions:
        config["spec_pdf_instructions"] = pdf_instructions

    if tg_config_dict:
        config.update(tg_config_dict)   # adds telegram_bot_token + telegram_chat_id

    _finalize_project(name, project_dir, config, title, venue_name, venue_format, venue_pages, pdf_spec)


# ============================================================
#  Deep Research helper
def _finalize_project(name: str, project_dir: Path, config: dict,
                      title: str, venue_name: str, venue_format: str,
                      venue_pages: int, pdf_spec: dict):
    """Create project directory, write config, copy templates, set up workspace."""
    code_dir = config["code_dir"]

    # ── Register project in webapp DB ──────────────────────────
    project_id = None
    try:
        from website.dashboard.db import (resolve_db_path, get_session,
                                   get_or_create_user_by_email, create_project as db_create_project)
        import getpass
        db_path = resolve_db_path()
        if db_path and Path(db_path).parent.exists():
            with get_session(db_path) as session:
                cli_email = f"{getpass.getuser()}@cli.local"
                user, _ = get_or_create_user_by_email(session, cli_email)
                import uuid
                project_id = str(uuid.uuid4())
                db_create_project(
                    session,
                    id=project_id,
                    user_id=user.id,
                    name=title or name,
                    title=title or "",
                    idea=config.get("research_idea", "") or config.get("idea", ""),
                    venue=venue_name,
                    venue_format=venue_format,
                    venue_pages=venue_pages,
                    max_iterations=config.get("max_iterations", 2),
                    max_dev_iterations=config.get("max_dev_iterations", 3),
                    mode=config.get("mode", "paper"),
                    model=config.get("model", "claude"),
                    model_variant=config.get("model_variant", ""),
                    code_dir=str(code_dir),
                    language=config.get("language", "en"),
                    source="cli",
                    status="queued",
                    telegram_token=config.get("telegram_bot_token", ""),
                    telegram_chat_id=config.get("telegram_chat_id", ""),
                )
            # Store project_id in config so cmd_run can pass it to orchestrator
            config["_project_id"] = project_id
            config["_db_path"] = db_path
    except Exception as e:
        print(f"  {_c('Note:', Colors.DIM)} DB registration skipped: {e}")

    # ── Create project directory ──────────────────────────────
    project_dir.mkdir(parents=True, exist_ok=True)
    agents_dir = project_dir / "agents"
    agents_dir.mkdir(exist_ok=True)

    # Write config
    config_file = project_dir / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # Copy template agent prompts
    templates_dir = Path(__file__).parent / "templates" / "agents"
    if templates_dir.exists():
        for prompt_file in templates_dir.glob("*.prompt"):
            dest = agents_dir / prompt_file.name
            content = prompt_file.read_text()
            content = content.replace("{PROJECT_NAME}", name)
            content = content.replace("{PAPER_TITLE}", title or name)
            content = content.replace("{VENUE_NAME}", venue_name)
            content = content.replace("{VENUE_FORMAT}", venue_format)
            content = content.replace("{VENUE_PAGES}", str(venue_pages))
            content = content.replace("{LATEX_DIR}", config["latex_dir"])
            content = content.replace("{FIGURES_DIR}", config["figures_dir"])
            dest.write_text(content)

    # Create default hooks.py
    hooks_template = Path(__file__).parent / "templates" / "hooks.py"
    if hooks_template.exists():
        shutil.copy(hooks_template, project_dir / "hooks.py")
    else:
        (project_dir / "hooks.py").write_text(
            "# Project-specific hooks\n"
            "# Override these functions to customize behavior\n\n"
            "def run_research_iteration(orch) -> bool:\n"
            '    """Custom research iteration logic."""\n'
            "    raise NotImplementedError('Define your research iteration logic here')\n"
        )

    # Create auto_research dirs in code_dir
    state_dir = os.path.join(code_dir, "auto_research", "state")
    os.makedirs(state_dir, exist_ok=True)
    os.makedirs(os.path.join(code_dir, "auto_research", "logs"), exist_ok=True)

    # Seed dev_state.yaml from PDF spec (if coding tasks were extracted)
    if pdf_spec.get("coding_tasks"):
        dev_state = {
            "spec_loaded": True,
            "spec": pdf_spec.get("raw_text", "")[:5000],
            "current_phase": "planning",
            "tasks": [
                {
                    "id": f"T{i+1}",
                    "title": task,
                    "description": task,
                    "status": "pending",
                    "priority": "medium",
                    "depends_on": [],
                }
                for i, task in enumerate(pdf_spec["coding_tasks"])
            ],
            "test_history": [],
            "code_review_scores": [],
            "last_test_results": {},
        }
        dev_state_file = os.path.join(state_dir, "dev_state.yaml")
        with open(dev_state_file, "w") as f:
            yaml.dump(dev_state, f, default_flow_style=False, allow_unicode=True)
        print(f"  {_c('Dev plan seeded:', Colors.GREEN)} {len(pdf_spec['coding_tasks'])} tasks from PDF")

    # Create symlink to auto_research under projects/<name>/
    ensure_project_symlinks(project_dir, code_dir)

    # Set up LaTeX template
    _setup_latex_template(code_dir, config)

    print()
    # Completion banner
    w = 44
    print(f"  {_c('╔' + '═' * w + '╗', Colors.GREEN)}")
    print(f"  {_c('║', Colors.GREEN)}  {_c('✓', Colors.GREEN)}  Project {_c(repr(name), Colors.BOLD)} created!{' ' * max(0, w - 18 - len(name))} {_c('║', Colors.GREEN)}")
    print(f"  {_c('╠' + '═' * w + '╣', Colors.GREEN)}")
    if config.get("mode") == "dev":
        run_cmd = f"ark run {name} --mode dev"
    else:
        run_cmd = f"ark run {name}"
    lines = [
        (run_cmd, "Start project"),
        (f"ark config {name}", "Edit config"),
        (f"ark status", "Check status"),
    ]
    for cmd, desc in lines:
        padded = f"  {_c(cmd, Colors.BOLD):<50}{desc}"
        print(f"  {_c('║', Colors.GREEN)} {padded}{' ' * max(0, w - 2 - len(cmd) - len(desc) - 4)} {_c('║', Colors.GREEN)}")
    print(f"  {_c('╚' + '═' * w + '╝', Colors.GREEN)}")

    print()


# ============================================================
#  Deep Research helper
# ============================================================

def _run_deep_research_for_project(config: dict, state_dir: Path, custom_query: str = None):
    """Run Gemini Deep Research for a project if GEMINI_API_KEY is set."""
    from ark.deep_research import run_deep_research, get_gemini_api_key

    print()
    print(f"{_c('Deep Research (Gemini)', Colors.BOLD)}")

    api_key = get_gemini_api_key()
    if not api_key:
        print(f"  {_c('Skipped: no GEMINI_API_KEY in environment.', Colors.YELLOW)}")
        print(f"  {_c('Export it before running: export GEMINI_API_KEY=YOUR_KEY', Colors.DIM)}")
        print()
        return None

    # Allow custom query interactively
    if custom_query is None and sys.stdin.isatty():
        print("  Enter a custom research query, or press Enter for auto-generated:")
        raw = input("  > ").strip()
        if raw:
            custom_query = raw

    report_path = run_deep_research(
        config=config,
        output_dir=state_dir,
        custom_query=custom_query,
        api_key=api_key,
    )

    if report_path:
        print(f"  {_c('Deep Research report ready.', Colors.GREEN)}")
    else:
        print(f"  {_c('Deep Research failed.', Colors.YELLOW)}")
    print()
    return report_path


# ============================================================
#  ark research
# ============================================================

def cmd_research(args):
    """Run or re-run Gemini Deep Research for a project."""
    name = args.project
    config = get_project_config(name)

    if not config:
        print(f"{_c('Error:', Colors.RED)} Project '{name}' not found.")
        sys.exit(1)

    code_dir = config.get("code_dir", "")
    if not code_dir:
        print(f"{_c('Error:', Colors.RED)} No code_dir configured for project '{name}'.")
        sys.exit(1)

    state_dir = Path(code_dir) / "auto_research" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    deep_research_file = state_dir / "deep_research.md"

    if deep_research_file.exists() and not args.force:
        print(f"  Deep Research report already exists: {deep_research_file}")
        if sys.stdin.isatty():
            if prompt_yn("Re-run and overwrite?", default=False):
                deep_research_file.unlink()
            else:
                print("  Use --force to overwrite without asking.")
                return
        else:
            print(f"  Use --force to overwrite.")
            return
    elif deep_research_file.exists() and args.force:
        deep_research_file.unlink()

    _run_deep_research_for_project(config, state_dir, custom_query=args.query)


# ============================================================
#  ark run
# ============================================================

def cmd_run(args):
    name = args.project
    config = get_project_config(name)

    if not config:
        print(f"{_c('Error:', Colors.RED)} Project '{name}' not found. Run 'ark new {name}' first.")
        sys.exit(1)

    project_dir = get_projects_dir() / name
    pid_file = project_dir / ".pid"

    # Check if already running
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            print(f"{_c('Warning:', Colors.YELLOW)} Project '{name}' is already running (PID {pid})")
            print(f"Use 'ark stop {name}' to stop it first, or 'ark status {name}' to check.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)

    code_dir = config.get("code_dir", str(get_ark_root().parent))
    model = args.model or config.get("model", "claude")
    mode = args.mode or config.get("mode", "paper")
    max_iterations = args.iterations or 3
    max_days = args.max_days or 3

    # ── Deep Research (runs in background inside orchestrator) ─
    state_dir = Path(code_dir) / "auto_research" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    deep_research_file = state_dir / "deep_research.md"

    if args.no_research:
        config["skip_deep_research"] = True
        print(f"  Deep Research: {_c('disabled', Colors.DIM)} (--no-research)")
    elif deep_research_file.exists():
        print(f"  Deep Research: {_c('report exists', Colors.GREEN)}")
    else:
        print(f"  Deep Research: {_c('will run in background', Colors.CYAN)}")
    print()

    # ── Launch orchestrator ───────────────────────────────────
    log_dir = Path(code_dir) / "auto_research" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Ensure symlinks exist
    ensure_project_symlinks(project_dir, code_dir)

    log_file = log_dir / f"{name}_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    print(f"Starting {_c(name, Colors.BOLD)} in {_c(mode, Colors.CYAN)} mode...")
    print(f"  Model:          {model}")
    print(f"  Code dir:       {code_dir}")
    print(f"  Max iterations: {max_iterations}")
    print(f"  Max days:       {max_days}")
    print(f"  Log file:       {log_file}")

    # ── Resolve DB path and project ID for orchestrator ──
    db_path = config.get("_db_path", "")
    project_id = config.get("_project_id", "")
    if not db_path:
        try:
            from website.dashboard.db import resolve_db_path
            db_path = resolve_db_path()
        except Exception:
            pass
    if not project_id and db_path and Path(db_path).exists():
        try:
            from website.dashboard.db import get_session, get_project_by_name
            with get_session(db_path) as session:
                p = get_project_by_name(session, name)
                if p:
                    project_id = p.id
        except Exception:
            pass

    # Launch orchestrator in background, preferring per-project conda env
    try:
        from website.dashboard.jobs import (
            find_conda_binary, project_env_ready, project_env_prefix,
        )
        conda_bin = find_conda_binary()
        if conda_bin and project_env_ready(project_dir):
            python_prefix = [conda_bin, "run", "--no-capture-output",
                             "--prefix", str(project_env_prefix(project_dir)),
                             "python"]
            print(f"  Conda env:      {project_env_prefix(project_dir)}")
        elif conda_bin and config.get("conda_env"):
            python_prefix = [conda_bin, "run", "--no-capture-output",
                             "-n", config["conda_env"], "python"]
            print(f"  Conda env:      {config['conda_env']}")
        else:
            python_prefix = [sys.executable]
    except ImportError:
        python_prefix = [sys.executable]

    cmd = python_prefix + [
        "-m", "ark.orchestrator",
        "--project", name,
        "--mode", mode,
        "--model", model,
        "--iterations", str(max_iterations),
        "--max-days", str(max_days),
    ]
    if db_path:
        cmd.extend(["--db-path", db_path])
    if project_id:
        cmd.extend(["--project-id", project_id])

    # Strip CLAUDECODE so orchestrator can call claude CLI freely
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    # Ensure orchestrator can find the ark + website packages.
    # parents[1] is the ARK repo root (.../ARK/ark/cli.py → .../ARK/ark → .../ARK).
    # Previously this had `.parent` appended which dropped one level too high
    # and broke fresh projects without a conda env that already had ARK on
    # sys.path via a .pth file.
    ark_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = ark_root + ((":" + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")

    with open(log_file, "w") as lf:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,  # Don't hold terminal pty fd
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # Detach from terminal
            cwd=code_dir,
            env=env,
        )

    # Save PID
    pid_file.write_text(str(process.pid))

    print()
    print(f"{_c('Started!', Colors.GREEN)} PID: {process.pid}")
    print(f"  Check progress: ark status {name}")
    print(f"  Send updates:   ark update {name}")
    print(f"  Stop:           ark stop {name}")
    print(f"  View logs:      tail -f {log_file}")

    print()


# ============================================================
#  ark status
# ============================================================

def cmd_status(args):
    projects_dir = get_projects_dir()

    if args.project:
        _show_project_status(args.project)
    else:
        # Show all projects
        if not projects_dir.exists():
            print("No projects found.")
            return

        project_names = sorted([
            d.name for d in projects_dir.iterdir()
            if d.is_dir() and (d / "config.yaml").exists()
        ])

        if not project_names:
            print("No projects found. Run 'ark new <name>' to create one.")
            return

        # Group by running / stopped
        running_projects = []
        stopped_projects = []
        for name in project_names:
            project_dir = projects_dir / name
            is_run, _ = _is_running(project_dir)
            if is_run:
                running_projects.append(name)
            else:
                stopped_projects.append(name)

        print()
        print(f"  {Icons.SPARKLE} {_c('ARK Projects', Colors.BOLD)}  ({len(project_names)} total)")
        print(f"  {'─' * 60}")

        if running_projects:
            print(f"  {_c(f'{Icons.RUNNING} Running', Colors.GREEN + Colors.BOLD)} ({len(running_projects)})")
            for name in running_projects:
                _show_project_status_brief(name)

        if stopped_projects:
            if running_projects:
                print()
            print(f"  {_c(f'{Icons.STOPPED} Stopped', Colors.DIM + Colors.BOLD)} ({len(stopped_projects)})")
            for name in stopped_projects:
                _show_project_status_brief(name)

        _webapp_db = get_config_dir() / "webapp.db"
        _disabled_flag = get_ark_root() / "ark_webapp" / "disabled"
        if _webapp_db.exists():
            import sqlite3 as _sq
            try:
                con = _sq.connect(str(_webapp_db))
                rows = con.execute("""
                    SELECT p.id, p.name, p.title, p.venue, p.status, p.score,
                           COALESCE(u.name, substr(p.user_id,1,8)) as username
                    FROM project p LEFT JOIN user u ON p.user_id = u.id
                    ORDER BY p.created_at DESC
                """).fetchall()
                con.close()
                print()
                if _disabled_flag.exists():
                    gate_s = _c("● DISABLED", Colors.RED + Colors.BOLD)
                    gate_hint = _c("  (ark web enable to reopen)", Colors.DIM)
                else:
                    gate_s = _c("● OPEN", Colors.GREEN + Colors.BOLD)
                    gate_hint = ""
                print(f"  {Icons.SPARKLE} {_c('Webapp', Colors.BOLD)}  gate {gate_s}{gate_hint}  {_c(f'({len(rows)} projects)', Colors.DIM)}")
                if rows:
                    print(f"  {'─' * 68}")
                    for pid, name, title, venue, status, score, username in rows:
                        icon = Icons.RUNNING if status == "running" else \
                               "⏳" if status in ("queued", "pending") else Icons.STOPPED
                        score_s = f"  {_c(f'{float(score):.1f}', Colors.BOLD)}/10" if float(score or 0) > 0 else ""
                        label = (title or name or pid)[:18]
                        status_disp = {"queued": "queued", "running": "running",
                                       "done": "done", "failed": "failed",
                                       "stopped": "stopped", "pending": "pending",
                                       "waiting_template": "await-tmpl"}.get(status, status)
                        user_s = _c(f"{(username or '?')[:10]:<10}", Colors.DIM)
                        print(f"    {icon} {user_s} {_c(label, Colors.BOLD):<20} {(venue or '?'):<10} {status_disp:<12}{score_s}")
            except Exception:
                pass

        print()


def _is_running(project_dir: Path) -> tuple:
    """Check if project is running. Returns (running, pid)."""
    pid_file = project_dir / ".pid"
    if not pid_file.exists():
        return False, None
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True, pid
    except (ProcessLookupError, ValueError, PermissionError):
        pid_file.unlink(missing_ok=True)
        return False, None


def _show_project_status_brief(name: str):
    """Show one-line status for a project."""
    project_dir = get_projects_dir() / name
    config = get_project_config(name)
    running, pid = _is_running(project_dir)

    icon = Icons.RUNNING if running else Icons.STOPPED
    model = config.get("model", "?")
    venue = config.get("venue", "?")

    # Try to read score from state
    code_dir = config.get("code_dir", "")
    score_str = ""
    spark = ""
    if code_dir:
        memory_file = Path(code_dir) / "auto_research" / "state" / "memory.yaml"
        if memory_file.exists():
            try:
                with open(memory_file) as f:
                    mem = yaml.safe_load(f) or {}
                scores = mem.get("scores", [])
                if scores:
                    score_str = f"  {_c(f'{scores[-1]:.1f}', Colors.BOLD)}/10"
                    spark = f" {score_sparkline(scores)}" if len(scores) > 1 else ""
            except Exception:
                pass

    pid_str = f"  PID:{pid}" if running else ""
    print(f"    {icon} {_c(name, Colors.BOLD):<20} {venue:<12} {model:<8}{score_str}{spark}{pid_str}")


def _fmt_elapsed(seconds: float) -> str:
    """Format elapsed seconds into a human-readable string."""
    if seconds < 60:
        return f"{int(seconds)}s ago"
    elif seconds < 3600:
        m = int(seconds // 60)
        return f"{m}m ago"
    elif seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h{m}m ago" if m else f"{h}h ago"
    else:
        d = int(seconds // 86400)
        h = int((seconds % 86400) // 3600)
        return f"{d}d{h}h ago"


def _parse_live_info(lines: list) -> dict:
    """Parse log lines to extract live execution info.

    Returns dict with keys:
        iteration: current iteration string (e.g. "3/100")
        phase: current phase string (e.g. "PHASE 4/6: Plan & Execute Improvements")
        agent: active agent call dict or None
            {"type": "experimenter", "start_time": "22:18:14", "elapsed_str": "3m 22s"}
        model: model name if detectable
        rate_limit: rate limit wait info if any
    """
    info = {"iteration": "", "phase": "", "agent": None, "rate_limit": ""}

    # Track agent start/complete to find active call
    last_agent_start = None  # (agent_type, timestamp_str, line_idx)
    last_agent_complete = None  # (agent_type, line_idx)

    for i, line in enumerate(lines):
        # Iteration header
        m = re.search(r"ITERATION\s+(\d+/\d+)", line)
        if m:
            info["iteration"] = m.group(1)

        # Phase header
        m = re.search(r"PHASE\s+(\d+/\d+):\s*(.+?)[\s─]*$", line)
        if m:
            info["phase"] = f"Phase {m.group(1)}: {m.group(2).strip()}"

        # Agent start: "→ Agent [type] →"
        m = re.search(r"\[(\d{2}:\d{2}:\d{2})\].*?Agent\s+\[(\w+)\]\s*→", line)
        if m:
            last_agent_start = (m.group(2), m.group(1), i)

        # Agent complete: "✓ Agent [type] completed"
        m = re.search(r"Agent\s+\[(\w+)\]\s+completed", line)
        if m:
            last_agent_complete = (m.group(1), i)

        # Rate limit
        m = re.search(r"Rate Limit.*?waiting\s*([\d.]+)\s*minutes", line)
        if m:
            info["rate_limit"] = f"Rate limited, waiting {m.group(1)}min"
        m = re.search(r"waiting\s*([\d.]+)\s*minutes before auto-recovery", line)
        if m:
            info["rate_limit"] = f"Rate limited, resuming in {m.group(1)}min"

    # Determine if an agent is currently active
    if last_agent_start:
        agent_type, start_ts, start_idx = last_agent_start
        # Check if this agent was completed after it started
        is_active = True
        if last_agent_complete:
            comp_type, comp_idx = last_agent_complete
            if comp_idx > start_idx:
                is_active = False

        if is_active:
            # Calculate elapsed time
            elapsed_str = ""
            try:
                now = datetime.now()
                h, m_val, s = map(int, start_ts.split(":"))
                start_dt = now.replace(hour=h, minute=m_val, second=s, microsecond=0)
                if start_dt > now:
                    start_dt = start_dt.replace(day=start_dt.day - 1)
                elapsed = (now - start_dt).total_seconds()
                if elapsed < 60:
                    elapsed_str = f"{int(elapsed)}s"
                elif elapsed < 3600:
                    elapsed_str = f"{int(elapsed//60)}m{int(elapsed%60)}s"
                else:
                    elapsed_str = f"{int(elapsed//3600)}h{int((elapsed%3600)//60)}m"
            except Exception:
                pass

            info["agent"] = {
                "type": agent_type,
                "start_time": start_ts,
                "elapsed_str": elapsed_str,
            }

    return info


def _dedup_tail(lines: list, n: int = 15) -> list:
    """Get last n non-duplicate, non-empty lines."""
    seen = set()
    result = []
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Normalize whitespace for dedup
        key = " ".join(stripped.split())
        if key not in seen:
            seen.add(key)
            result.append(line)
            if len(result) >= n:
                break
    result.reverse()
    return result


def _gather_status_context(name: str, code_dir: str) -> str:
    """Gather project state data into a text context for AI summary."""
    parts = []
    state_dir = Path(code_dir) / "auto_research" / "state"

    # Memory (scores, stagnation, issues)
    memory_file = state_dir / "memory.yaml"
    if memory_file.exists():
        try:
            with open(memory_file) as f:
                mem = yaml.safe_load(f) or {}
            scores = mem.get("scores", [])
            best = mem.get("best_score", 0)
            stag = mem.get("stagnation_count", 0)
            issue_hist = mem.get("issue_history", {})
            last_issues = mem.get("last_issues", [])
            repair_eff = mem.get("repair_effective", None)
            parts.append(f"Scores: {scores}")
            parts.append(f"Best score: {best}, Stagnation count: {stag}")
            if last_issues:
                parts.append(f"Last issues: {last_issues}")
            if issue_hist:
                repeat = {k: v for k, v in issue_hist.items() if v >= 3}
                if repeat:
                    parts.append(f"Repeating issues (3+): {repeat}")
            if repair_eff is not None:
                parts.append(f"Last repair effective: {repair_eff}")
        except Exception:
            pass

    # Checkpoint
    ckpt_file = state_dir / "checkpoint.yaml"
    if ckpt_file.exists():
        try:
            with open(ckpt_file) as f:
                ckpt = yaml.safe_load(f) or {}
            parts.append(f"Checkpoint: iteration={ckpt.get('iteration', '?')}, timestamp={ckpt.get('timestamp', '?')}")
        except Exception:
            pass

    # Latest review (truncated)
    review_file = state_dir / "latest_review.md"
    if review_file.exists():
        try:
            content = review_file.read_text()
            if len(content) > 3000:
                content = content[:3000] + "\n...(truncated)"
            parts.append(f"Latest review:\n{content}")
        except Exception:
            pass

    # Action plan (truncated)
    plan_file = state_dir / "action_plan.yaml"
    if plan_file.exists():
        try:
            content = plan_file.read_text()
            if len(content) > 1500:
                content = content[:1500] + "\n...(truncated)"
            parts.append(f"Action plan:\n{content}")
        except Exception:
            pass

    # Recent log tail
    log_dir = Path(code_dir) / "auto_research" / "logs"
    if log_dir.exists():
        logs = sorted(log_dir.glob(f"{name}_*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        if logs:
            try:
                all_lines = logs[0].read_text().strip().split("\n")
                tail = all_lines[-40:]
                parts.append(f"Recent log ({logs[0].name}):\n" + "\n".join(tail))
            except Exception:
                pass

    return "\n\n".join(parts)


def _get_state_fingerprint(code_dir: str, name: str) -> str:
    """Get a fingerprint of state files to detect changes."""
    import hashlib
    state_dir = Path(code_dir) / "auto_research" / "state"
    log_dir = Path(code_dir) / "auto_research" / "logs"
    fp_parts = []
    for fname in ["memory.yaml", "checkpoint.yaml", "latest_review.md", "action_plan.yaml"]:
        f = state_dir / fname
        if f.exists():
            try:
                fp_parts.append(f"{fname}:{f.stat().st_mtime:.0f}")
            except Exception:
                pass
    # Also include latest log mtime
    if log_dir.exists():
        logs = sorted(log_dir.glob(f"{name}_*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        if logs:
            try:
                fp_parts.append(f"log:{logs[0].stat().st_mtime:.0f}")
            except Exception:
                pass
    return hashlib.md5("|".join(fp_parts).encode()).hexdigest()[:12]


def _generate_ai_summary(name: str, config: dict, code_dir: str, running: bool) -> str:
    """Generate AI summary of project status using claude CLI."""
    context = _gather_status_context(name, code_dir)
    if not context.strip():
        return ""

    venue = config.get("venue", "?")
    title = config.get("title", "?")

    prompt = f"""You are summarizing a research project's status for a CLI dashboard.

Project: {name}
Title: {title}
Venue: {venue}
Running: {running}

State data:
{context}

Write a concise status summary in 3-5 sentences in English. Cover:
1. Current progress (iteration, score trend)
2. Key problems/blockers (repeating issues, stagnation)
3. What's happening now or what happened last

Be direct and specific. No greetings, no markdown headers. Just the summary text."""

    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "-p", prompt,
             "--model", _get_configured_model(),
             "--no-session-persistence",
             "--output-format", "text"],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL, env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _get_cached_summary(name: str, code_dir: str, config: dict, running: bool) -> str:
    """Get AI summary, using cache if state hasn't changed."""
    state_dir = Path(code_dir) / "auto_research" / "state"
    cache_file = state_dir / ".status_cache"

    fingerprint = _get_state_fingerprint(code_dir, name)

    # Try to read cache
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                cache = yaml.safe_load(f) or {}
            if cache.get("fingerprint") == fingerprint and cache.get("summary"):
                return cache["summary"]
        except Exception:
            pass

    # Generate new summary
    summary = _generate_ai_summary(name, config, code_dir, running)
    if summary:
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            with open(cache_file, "w") as f:
                yaml.dump({"fingerprint": fingerprint, "summary": summary}, f,
                          allow_unicode=True)
        except Exception:
            pass
    return summary


def _show_project_status(name: str):
    """Show project status with AI summary."""
    project_dir = get_projects_dir() / name
    config = get_project_config(name)

    if not config:
        print(f"{_c('Error:', Colors.RED)} Project '{name}' not found.")
        sys.exit(1)

    running, pid = _is_running(project_dir)
    code_dir = config.get("code_dir", "")

    print()
    print(f"  {_c(f'Project: {name}', Colors.BOLD + Colors.CYAN)}")
    print(f"  {'─' * 50}")

    # ── Status line ───────────────────────────────────────────
    status_icon = Icons.RUNNING if running else Icons.STOPPED
    status_label = _c('RUNNING', Colors.GREEN) if running else _c('STOPPED', Colors.DIM)
    pid_label = f" (PID {pid})" if running else ""
    print(f"  Status:    {status_icon} {status_label}{pid_label}")
    print(f"  Venue:     {config.get('venue', 'N/A')}  |  Model: {config.get('model', 'N/A')}")
    title = config.get('title', 'N/A') or 'N/A'
    if len(title) > 60:
        title = title[:57] + "..."
    print(f"  Title:     {title}")

    if not code_dir:
        print()
        return

    # ── Score & Progress (compact) ────────────────────────────
    scores = []
    best = 0
    stag = 0
    memory_file = Path(code_dir) / "auto_research" / "state" / "memory.yaml"
    if memory_file.exists():
        try:
            with open(memory_file) as f:
                mem = yaml.safe_load(f) or {}
            scores = mem.get("scores", [])
            best = mem.get("best_score", 0)
            stag = mem.get("stagnation_count", 0)
        except Exception:
            pass

    iteration = 0
    checkpoint_file = Path(code_dir) / "auto_research" / "state" / "checkpoint.yaml"
    if checkpoint_file.exists():
        try:
            with open(checkpoint_file) as f:
                ckpt = yaml.safe_load(f) or {}
            iteration = ckpt.get("iteration", 0)
        except Exception:
            pass

    if scores:
        current = scores[-1]
        spark = score_sparkline(scores)
        stag_str = f"  {_c(f'(stagnant {stag})', Colors.YELLOW)}" if stag > 0 else ""
        trend_str = ""
        if len(scores) >= 2:
            trend_str = f"  {score_trend(current, scores[-2])}"
        print(f"  Score:     {_c(f'{current:.1f}', Colors.BOLD)}/10  (best: {best:.1f})  iter: {iteration}{trend_str}{stag_str}")
        print(f"  History:   {spark}")
    elif iteration > 0:
        print(f"  Iteration: {iteration}")

    # ── Issues summary ────────────────────────────────────────
    review_file = Path(code_dir) / "auto_research" / "state" / "latest_review.md"
    if review_file.exists():
        try:
            content = review_file.read_text()
            major = re.findall(r"### M\d+\.", content)
            minor = re.findall(r"### m\d+\.", content)
            if major or minor:
                print(f"  Issues:    {len(major)} major, {len(minor)} minor")
        except Exception:
            pass

    # ── Live activity (one line) ──────────────────────────────
    log_dir = Path(code_dir) / "auto_research" / "logs"
    if running and log_dir.exists():
        logs = sorted(log_dir.glob(f"{name}_*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        if logs:
            try:
                all_lines = logs[0].read_text().strip().split("\n")
                live = _parse_live_info(all_lines)
                parts = []
                if live["iteration"]:
                    parts.append(f"iter {live['iteration']}")
                if live["phase"]:
                    parts.append(live["phase"])
                if live["agent"]:
                    a = live["agent"]
                    agent_str = f"{Icons.for_agent(a['type'])} [{a['type']}]"
                    if a["elapsed_str"]:
                        agent_str += f" {a['elapsed_str']}"
                    parts.append(agent_str)
                if live["rate_limit"]:
                    parts.append(_c(live["rate_limit"], Colors.YELLOW))
                if parts:
                    print(f"  Activity:  {' | '.join(parts)}")
            except Exception:
                pass

    # ── AI Summary ────────────────────────────────────────────
    summary = _get_cached_summary(name, code_dir, config, running)
    if summary:
        print()
        print(f"  {_c('Summary', Colors.BOLD)}")
        for line in summary.split("\n"):
            line = line.strip()
            if line:
                # Wrap long lines
                wrapped = textwrap.wrap(line, width=70)
                for wl in wrapped:
                    print(f"  {wl}")

    print()
    if running:
        print(f"  {_c('Tip:', Colors.DIM)} Use 'ark monitor {name}' for live monitoring")
        print()


def _show_project_monitor(name: str):
    """Show full detailed status for a project (used by monitor)."""
    project_dir = get_projects_dir() / name
    config = get_project_config(name)

    if not config:
        print(f"{_c('Error:', Colors.RED)} Project '{name}' not found.")
        sys.exit(1)

    running, pid = _is_running(project_dir)
    code_dir = config.get("code_dir", "")

    now_str = datetime.now().strftime("%H:%M:%S")
    print(f"  {_c(f'Monitor: {name}', Colors.BOLD + Colors.CYAN)}  {_c(f'[{now_str}]', Colors.DIM)}")
    print(f"  {'─' * 50}")

    # ── Status line with activity indicator ───────────────────
    status_label = _c('RUNNING', Colors.GREEN) if running else _c('STOPPED', Colors.DIM)
    pid_label = f" (PID {pid})" if running else ""

    activity_info = ""
    if running and code_dir:
        log_dir = Path(code_dir) / "auto_research" / "logs"
        if log_dir.exists():
            logs = sorted(log_dir.glob(f"{name}_*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
            if logs:
                try:
                    mtime = logs[0].stat().st_mtime
                    elapsed = time.time() - mtime
                    elapsed_str = _fmt_elapsed(elapsed)
                    if elapsed < 30:
                        activity_info = f" | {_c(f'Active ({elapsed_str})', Colors.GREEN)}"
                    elif elapsed < 300:
                        activity_info = f" | {_c(f'Last output: {elapsed_str}', Colors.YELLOW)}"
                    else:
                        activity_info = f" | {_c(f'No output for {elapsed_str}', Colors.RED)}"
                except Exception:
                    pass

    print(f"  Status:    {status_label}{pid_label}{activity_info}")
    print(f"  Venue:     {config.get('venue', 'N/A')}  |  Model: {config.get('model', 'N/A')}")

    if not code_dir:
        return

    # ── Score ─────────────────────────────────────────────────
    memory_file = Path(code_dir) / "auto_research" / "state" / "memory.yaml"
    if memory_file.exists():
        try:
            with open(memory_file) as f:
                mem = yaml.safe_load(f) or {}
            scores = mem.get("scores", [])
            best = mem.get("best_score", 0)
            stag = mem.get("stagnation_count", 0)
            if scores:
                current = scores[-1]
                trend = " → ".join(f"{s:.1f}" for s in scores[-8:])
                stag_str = f"  {_c(f'(stagnant {stag})', Colors.YELLOW)}" if stag > 0 else ""
                print(f"  Score:     {_c(f'{current:.1f}', Colors.BOLD)}/10  (best: {best:.1f}){stag_str}")
                print(f"  Trend:     {trend}")
        except Exception:
            pass

    # ── Live Activity ─────────────────────────────────────────
    log_dir = Path(code_dir) / "auto_research" / "logs"
    all_lines = []
    latest_log = None
    if log_dir.exists():
        logs = sorted(log_dir.glob(f"{name}_*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        if logs:
            latest_log = logs[0]
            try:
                all_lines = latest_log.read_text().strip().split("\n")
            except Exception:
                pass

    if all_lines:
        live = _parse_live_info(all_lines)
        model_name = config.get("model", "?")

        if live["iteration"]:
            print(f"  Iteration: {_c(live['iteration'], Colors.BOLD)}")
        if live["phase"]:
            print(f"  Phase:     {_c(live['phase'], Colors.CYAN)}")

        if live["agent"]:
            agent = live["agent"]
            agent_icon = Icons.for_agent(agent['type'])
            call_label = f"{agent_icon} {model_name} [{agent['type']}]"
            if agent["elapsed_str"]:
                call_label += f" running for {agent['elapsed_str']}"
            print(f"  Agent:     {_c(call_label, Colors.GREEN + Colors.BOLD)}")
        elif running:
            print(f"  Agent:     {_c('idle (between calls)', Colors.DIM)}")

        if live["rate_limit"]:
            print(f"  {_c(live['rate_limit'], Colors.YELLOW)}")

    # ── Log tail ──────────────────────────────────────────────
    if all_lines:
        tail = _dedup_tail(all_lines, n=15)
        if tail:
            print(f"  {_c('─── Recent Output ───', Colors.DIM)}")
            for line in tail:
                print(f"  {_c('│', Colors.DIM)} {line[:120]}")

    # ── Pending user updates ──────────────────────────────────
    updates_file = Path(code_dir) / "auto_research" / "state" / "user_updates.yaml"
    if updates_file.exists():
        try:
            with open(updates_file) as f:
                updates = yaml.safe_load(f) or {}
            pending = [u for u in updates.get("updates", []) if not u.get("consumed")]
            if pending:
                print(f"  {_c(f'Pending Updates ({len(pending)})', Colors.YELLOW)}")
                for u in pending[-3:]:
                    print(f"  - {u.get('message', '')[:60]}")
        except Exception:
            pass


# ============================================================
#  ark monitor
# ============================================================

def cmd_monitor(args):
    """Live-monitor a project with auto-refresh."""
    name = args.project
    config = get_project_config(name)
    if not config:
        print(f"{_c('Error:', Colors.RED)} Project '{name}' not found.")
        sys.exit(1)

    interval = args.interval

    print(f"  Monitoring {_c(name, Colors.BOLD)}  (refresh every {interval}s, Ctrl+C to stop)")
    print()

    try:
        while True:
            # Clear screen
            os.system("clear" if os.name != "nt" else "cls")
            _show_project_monitor(name)
            print()
            print(f"  {_c(f'Refreshing every {interval}s — Ctrl+C to stop', Colors.DIM)}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n  Stopped monitoring {name}.")


# ============================================================
#  ark update
# ============================================================

def _classify_update_type(message: str) -> bool:
    """Classify whether a user update is a persistent instruction (True) or one-time action (False).
    Uses a quick Claude call for classification."""
    prompt = (
        "Classify this user message for a research automation pipeline.\n"
        "Is it a PERSISTENT instruction (a lasting rule about how to do the research, e.g. "
        "'use PyTorch', 'crawl real data from website X', 'always compare against baseline Y', "
        "'use 2 GPUs', 'write in formal style') "
        "or a ONE-TIME action (a temporary directive for the current iteration only, e.g. "
        "'skip experiments this round', 'rerun figure 3', 'fix the bug in section 2', "
        "'regenerate the abstract')?\n\n"
        f"Message: {message}\n\n"
        "Reply with exactly one word: PERSISTENT or ONETIME"
    )
    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "--print", "--model", "claude-haiku-4-5", "-p", prompt],
            capture_output=True, text=True, timeout=30, env=env,
        )
        answer = result.stdout.strip().upper()
        return "PERSISTENT" in answer
    except Exception:
        # Default to persistent (safer — won't lose instructions)
        return True


def cmd_update(args):
    name = args.project
    config = get_project_config(name)

    if not config:
        print(f"{_c('Error:', Colors.RED)} Project '{name}' not found.")
        sys.exit(1)

    code_dir = config.get("code_dir", "")
    if not code_dir:
        print(f"{_c('Error:', Colors.RED)} No code_dir configured for project '{name}'.")
        sys.exit(1)

    updates_file = Path(code_dir) / "auto_research" / "state" / "user_updates.yaml"
    updates_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing updates
    existing = {"updates": []}
    if updates_file.exists():
        try:
            with open(updates_file) as f:
                existing = yaml.safe_load(f) or {"updates": []}
        except Exception:
            pass

    if args.message:
        # Non-interactive: use -m flag
        message = args.message
    else:
        # Interactive: prompt for input
        print(f"Update project {_c(name, Colors.BOLD)}:")
        print("Enter your update (what should the system focus on, change, or prioritize).")
        print("Press Enter twice to submit, Ctrl+C to cancel.")
        print()
        lines = []
        try:
            while True:
                line = input("  > ")
                if not line and lines and not lines[-1]:
                    break
                lines.append(line)
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return

        message = "\n".join(lines).strip()
        if not message:
            print("No update provided.")
            return

    # Classify: one-time action or persistent instruction?
    is_persistent = _classify_update_type(message)

    # Always write to user_updates (immediate effect next iteration)
    existing["updates"].append({
        "message": message,
        "timestamp": datetime.now().isoformat(),
        "consumed": False,
    })
    with open(updates_file, "w") as f:
        yaml.dump(existing, f, default_flow_style=False, allow_unicode=True)

    # If persistent, also save to user_instructions
    if is_persistent:
        instructions_file = Path(code_dir) / "auto_research" / "state" / "user_instructions.yaml"
        instr_data = {}
        if instructions_file.exists():
            try:
                with open(instructions_file) as f:
                    instr_data = yaml.safe_load(f) or {}
            except Exception:
                pass
        entries = instr_data.get("instructions", [])
        entries.append({
            "message": message,
            "source": "cli_update",
            "timestamp": datetime.now().isoformat(),
        })
        instr_data["instructions"] = entries
        with open(instructions_file, "w") as f:
            yaml.dump(instr_data, f, default_flow_style=False, allow_unicode=True)

    project_dir = get_projects_dir() / name
    running, _ = _is_running(project_dir)

    update_type = "persistent instruction" if is_persistent else "one-time action"
    print(f"{_c('Update saved!', Colors.GREEN)} (classified as {_c(update_type, Colors.CYAN)})")
    if running:
        print("The running orchestrator will pick it up at the next iteration.")
    else:
        print(f"Start the project with 'ark run {name}' to apply it.")


# ============================================================
#  ark stop
# ============================================================

def _cmd_setup_bot_project(project_name: str):
    """Set up Telegram bot for a specific project (saves to project config.yaml)."""
    project_dir = get_projects_dir() / project_name
    config_file = project_dir / "config.yaml"

    if not config_file.exists():
        print(f"  {_c(f'Project not found: {project_name}', Colors.RED)}")
        return

    print()
    print(f"  {_c(f'Telegram Setup for project: {project_name}', Colors.BOLD + Colors.CYAN)}")
    print()

    config = get_project_config(project_name)
    existing_token = config.get("telegram_bot_token", "")

    if existing_token:
        print(f"  {_c('Existing bot token found.', Colors.YELLOW)}")
        if not prompt_yn("  Replace it?", default=False):
            tg_token = existing_token
        else:
            tg_token = prompt_input("  New Bot Token").strip()
    else:
        print(f"  Get your Bot Token from @BotFather in Telegram (/newbot).")
        tg_token = prompt_input("  Bot Token").strip()

    if not tg_token:
        print("Aborted.")
        return

    # Auto-detect chat_id via getUpdates
    print()
    print(f"  {_c('Now go to Telegram and send any message to your bot.', Colors.BOLD)}")
    input("  Press Enter when done... ")

    import json as _json, urllib.request as _ur, time as _time
    tg_chat_id = None
    for attempt in range(3):
        try:
            url = f"https://api.telegram.org/bot{tg_token}/getUpdates"
            with _ur.urlopen(url, timeout=10) as resp:
                data = _json.loads(resp.read())
            results = data.get("result", [])
            if results:
                last = results[-1]
                msg = last.get("message") or last.get("edited_message") or {}
                sender = msg.get("from", {})
                tg_chat_id = str(sender.get("id") or msg.get("chat", {}).get("id", ""))
                if tg_chat_id:
                    print(f"  {_c(f'→ Chat ID detected: {tg_chat_id}', Colors.GREEN)}")
                    break
        except Exception as e:
            print(f"  {_c(f'Warning: {e}', Colors.YELLOW)}")
        if attempt < 2:
            print(f"  No messages found yet, retrying in 3s...")
            _time.sleep(3)

    if not tg_chat_id:
        print(f"  {_c('Could not auto-detect Chat ID.', Colors.YELLOW)}")
        tg_chat_id = prompt_input("  Enter Chat ID manually").strip()

    if not tg_chat_id:
        print("Aborted.")
        return

    # Send test message
    try:
        url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
        data = _json.dumps({
            "chat_id": tg_chat_id,
            "text": f"✅ ARK Bot configured for project '{project_name}'!",
        }).encode("utf-8")
        req = _ur.Request(url, data=data, headers={"Content-Type": "application/json"})
        _ur.urlopen(req, timeout=10)
        print(f"  {_c('→ Test message sent! Check your Telegram.', Colors.GREEN)}")
    except Exception as e:
        print(f"  {_c(f'Warning: test message failed: {e}', Colors.YELLOW)}")

    # Save to project config
    config["telegram_bot_token"] = tg_token
    config["telegram_chat_id"] = tg_chat_id
    with open(config_file, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print()
    print(f"  {_c(f'Saved to projects/{project_name}/config.yaml.', Colors.GREEN)}")
    print()


def cmd_setup_bot(args):
    project = getattr(args, 'project', None)
    if project:
        _cmd_setup_bot_project(project)
        return

    # Global telegram config is no longer supported. ARK is multi-tenant:
    # each project must have its own bot token, configured per-project.
    print()
    print(f"  {_c('Global telegram setup is no longer supported.', Colors.YELLOW)}")
    print()
    print(f"  ARK is multi-tenant — each project must have its own bot.")
    print(f"  Configure per-project:    {_c('ark setup-bot <project_name>', Colors.BOLD)}")
    print(f"  Or set env vars before running:")
    print(f"    {_c('export ARK_TELEGRAM_BOT_TOKEN=...', Colors.DIM)}")
    print(f"    {_c('export ARK_TELEGRAM_CHAT_ID=...', Colors.DIM)}")
    print()


def cmd_stop(args):
    name = args.project
    project_dir = get_projects_dir() / name
    pid_file = project_dir / ".pid"

    if not pid_file.exists():
        print(f"Project '{name}' is not running (no PID file).")
        return

    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        pid_file.unlink(missing_ok=True)
        print(f"Invalid PID file, cleaned up.")
        return

    try:
        os.kill(pid, 0)  # Check if exists
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        print(f"Process {pid} is not running. Cleaned up PID file.")
        return

    print(f"Stopping {_c(name, Colors.BOLD)} (PID {pid})...")

    # Send SIGTERM first (graceful shutdown)
    os.kill(pid, signal.SIGTERM)

    # Wait a bit
    for _ in range(10):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
    else:
        # Force kill if still running
        print("  Sending SIGKILL...")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    pid_file.unlink(missing_ok=True)
    print(f"{_c('Stopped.', Colors.GREEN)}")



# ============================================================
#  ark restart
# ============================================================

def cmd_restart(args):
    """Restart a project."""
    name = args.project
    config = get_project_config(name)

    if not config:
        print(f"{_c('Error:', Colors.RED)} Project '{name}' not found.")
        sys.exit(1)

    project_dir = get_projects_dir() / name

    # 1. Stop project if running
    running, pid = _is_running(project_dir)
    if running:
        print(f"Stopping {_c(name, Colors.BOLD)} (PID {pid})...")
        _stop_project_if_running(project_dir, name)
    else:
        print(f"Project '{name}' is not running.")

    # 2. Re-run project (reuse cmd_run logic)
    print()
    # Build a minimal args namespace for cmd_run
    class RunArgs:
        project = name
        model = None
        mode = None
        iterations = None
        max_days = None
        no_research = False
    cmd_run(RunArgs())


# ============================================================
#  ark webapp
# ============================================================

def _get_prod_worktree_dir() -> Path:
    """Get prod worktree path, always relative to the real repo root (not a worktree)."""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True,
            cwd=Path(__file__).parent.parent,
        )
        if r.returncode == 0:
            # git-common-dir points to the real repo's .git/
            repo_root = Path(r.stdout.strip()).parent
            return repo_root / ".ark" / "prod"
    except Exception:
        pass
    return Path(__file__).parent.parent.resolve() / ".ark" / "prod"

_DEV_SERVICE = "ark-webapp-dev"
_PROD_SERVICE = "ark-webapp"
_DEV_PORT = 1027
_PROD_PORT = 9527


def _service_file_path(service_name: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{service_name}.service"


def _conda_env_python(env_name: str) -> str:
    """Return the python binary path for a conda environment."""
    conda_base = Path.home() / "miniforge3" / "envs" / env_name / "bin" / "python"
    if conda_base.exists():
        return str(conda_base)
    # Fallback: try anaconda3
    anaconda_base = Path.home() / "anaconda3" / "envs" / env_name / "bin" / "python"
    if anaconda_base.exists():
        return str(anaconda_base)
    return sys.executable  # last resort


def _generate_service_unit(host: str, port: int, work_dir: Path, description: str,
                           env_vars=None, python_bin: str = None) -> str:
    """Generate a systemd user service unit file for the ARK webapp."""
    if python_bin is None:
        python_bin = sys.executable
    env_lines = ""
    if env_vars:
        for k, v in env_vars.items():
            env_lines += f"Environment={k}={v}\n"
    return f"""\
[Unit]
Description={description}
After=network.target

[Service]
Type=simple
WorkingDirectory={work_dir}
ExecStart={python_bin} -m ark.cli webapp --host {host} --port {port}
Restart=on-failure
RestartSec=5
{env_lines}StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def _check_linger():
    """Print a warning if linger is not enabled."""
    import subprocess as _sp
    try:
        r = _sp.run(["loginctl", "show-user", os.environ.get("USER", "")],
                     capture_output=True, text=True)
        if "Linger=no" in r.stdout:
            user = os.environ.get("USER", "")
            print(f"  {_c('Note:', Colors.YELLOW)} Linger is not enabled. Service stops on logout.")
            print(f"    {_c(f'loginctl enable-linger {user}', Colors.BOLD)}")
            print()
    except FileNotFoundError:
        pass


def _cmd_webapp_install(host: str, port: int, dev: bool = False):
    """Install and start ARK webapp as a systemd user service."""
    import subprocess as _sp

    from ark.paths import get_primary_ip

    # Shared data directory for both dev and prod
    data_dir = get_ark_root() / ".ark" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "webapp.db"

    if dev:
        svc_name = _DEV_SERVICE
        port = port if port != _PROD_PORT else _DEV_PORT
        work_dir = get_ark_root()
        desc = "ARK Research Portal (dev)"
        python_bin = _conda_env_python("ark-dev")
        conda_env = "ark-dev"

        # Install editable in ark-dev env
        print(f"  Installing dependencies (editable) in {conda_env}...")
        r = _sp.run(
            [python_bin, "-m", "pip", "install", "-e", ".[webapp]", "-q"],
            capture_output=True, text=True, cwd=work_dir,
        )
        if r.returncode != 0:
            print(f"  {_c(f'pip install warning: {r.stderr.strip()[:200]}', Colors.YELLOW)}")
    else:
        svc_name = _PROD_SERVICE
        work_dir = _get_prod_worktree_dir()
        desc = "ARK Research Portal"
        python_bin = _conda_env_python("ark-prod")

        # Ensure prod worktree exists
        if not work_dir.exists():
            print(f"  {_c('Error:', Colors.RED)} Prod worktree not found at {work_dir}")
            print(f"  Run {_c('ark webapp release', Colors.BOLD)} first to create it.")
            return

        # Symlink shared webapp.env into prod worktree
        prod_ark_dir = work_dir / ".ark"
        prod_ark_dir.mkdir(parents=True, exist_ok=True)
        prod_env_link = prod_ark_dir / "webapp.env"
        main_env = get_config_dir() / "webapp.env"
        if main_env.exists() and not prod_env_link.exists():
            prod_env_link.symlink_to(main_env)

    # Environment variables for systemd service.
    # Dashboard mount prefix is hardcoded in website/dashboard/constants.py
    # (DASHBOARD_PREFIX = "/dashboard") — no env var needed.
    env_vars = {
        "ARK_WEBAPP_DB_PATH": str(db_path),
        "PROJECTS_ROOT": str(data_dir / "projects"),
    }

    if dev:
        env_vars["ARK_SESSION_COOKIE"] = "session_dev"
        # Dev BASE_URL stays as the internal IP — dev is only reachable from
        # KAUST internal network, and magic-link emails from dev must be
        # clickable inside KAUST. No CF Tunnel routes dev.
        env_vars["BASE_URL"] = f"http://{get_primary_ip()}:{port}"
        # Dev does NOT support Google OAuth: Google rejects private IPs as
        # OAuth redirect URIs. Clear the creds so /auth/google/enabled
        # returns false and the UI hides the Google button.
        env_vars["GOOGLE_CLIENT_ID"] = ""
        env_vars["GOOGLE_CLIENT_SECRET"] = ""
        # Load dev-only env overrides
        dev_env_file = get_config_dir() / "webapp-dev.env"
        if not dev_env_file.exists():
            dev_env_file.write_text(
                "# Dev-only overrides (takes priority over webapp.env)\n"
                "# ALLOWED_EMAILS=user1@example.com,user2@example.com\n"
            )
            print(f"  Created {_c(str(dev_env_file), Colors.CYAN)} — edit to set dev-only config.")
        if dev_env_file.exists():
            for line in dev_env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env_vars[k.strip()] = v.strip()
    else:
        # Prod BASE_URL is the public origin; /dashboard prefix comes from
        # DASHBOARD_PREFIX constant in website/dashboard/constants.py.
        env_vars["BASE_URL"] = "https://idea2paper.org"

    svc_path = _service_file_path(svc_name)
    svc_path.parent.mkdir(parents=True, exist_ok=True)

    unit = _generate_service_unit(host, port, work_dir, desc, env_vars, python_bin=python_bin)
    svc_path.write_text(unit)
    print(f"  Service file written to {_c(str(svc_path), Colors.CYAN)}")

    _sp.run(["systemctl", "--user", "daemon-reload"], check=True)
    _sp.run(["systemctl", "--user", "enable", svc_name], check=True)
    _sp.run(["systemctl", "--user", "start", svc_name], check=True)

    label = "Dev" if dev else "Prod"
    print(f"\n  {_c(f'ARK Webapp ({label})', Colors.BOLD)} installed and started.")
    print(f"  URL: {_c(f'http://{host}:{port}', Colors.CYAN)}")
    print(f"  DB:  {_c(str(db_path), Colors.DIM)}")
    print()
    print(f"  Manage with:")
    print(f"    {_c(f'ark webapp status', Colors.DIM)}")
    print(f"    {_c(f'ark webapp restart', Colors.DIM)}")
    print(f"    {_c(f'ark webapp logs -f', Colors.DIM)}")
    print()

    # Auto-enable linger so service persists after logout
    import pwd as _pwd
    _user = _pwd.getpwuid(os.getuid()).pw_name
    _lr = _sp.run(["loginctl", "enable-linger", _user], capture_output=True)
    if _lr.returncode == 0:
        print(f"  {_c('Linger enabled', Colors.GREEN)} — service will persist after logout.")
    else:
        _check_linger()


def _cmd_webapp_uninstall(dev: bool = False):
    """Stop and remove the ARK webapp systemd user service."""
    import subprocess as _sp

    svc_name = _DEV_SERVICE if dev else _PROD_SERVICE
    svc_path = _service_file_path(svc_name)
    if not svc_path.exists():
        label = "dev" if dev else "prod"
        print(f"  {_c('Not installed:', Colors.YELLOW)} No {label} service found at {svc_path}")
        return

    _sp.run(["systemctl", "--user", "stop", svc_name], capture_output=True)
    _sp.run(["systemctl", "--user", "disable", svc_name], capture_output=True)
    svc_path.unlink(missing_ok=True)
    _sp.run(["systemctl", "--user", "daemon-reload"], check=True)

    label = "Dev" if dev else "Prod"
    print(f"  {_c(f'ARK Webapp ({label})', Colors.BOLD)} service stopped and removed.")


def _cmd_webapp_status(dev: bool = False):
    """Show systemd service status for the ARK webapp."""
    import subprocess as _sp
    svc = _DEV_SERVICE if dev else _PROD_SERVICE
    _sp.run(["systemctl", "--user", "status", svc])


def _cmd_webapp_logs(dev: bool = False, follow: bool = False, lines: int = 50):
    """Show service logs via journalctl."""
    import subprocess as _sp
    svc = _DEV_SERVICE if dev else _PROD_SERVICE
    cmd = ["journalctl", "--user", "-u", svc, f"-n{lines}"]
    if follow:
        cmd.append("-f")
    _sp.run(cmd)


def _cmd_webapp_restart(dev: bool = False):
    """Restart the ARK webapp systemd service."""
    import subprocess as _sp
    svc = _DEV_SERVICE if dev else _PROD_SERVICE
    r = _sp.run(["systemctl", "--user", "restart", svc])
    if r.returncode == 0:
        label = "Dev" if dev else "Prod"
        print(f"  {_c(f'ARK Webapp ({label})', Colors.BOLD)} restarted.")
    else:
        print(f"  {_c('Error:', Colors.RED)} Failed to restart {svc}. Is it installed?")
        print(f"  Run: {_c('ark webapp install', Colors.BOLD)}")


def _cmd_webapp_release(args):
    """Tag current commit, create/update prod worktree, restart prod service."""
    import subprocess as _sp

    ark_root = get_ark_root()

    # 1. Determine version tag
    # Find latest vX.Y.Z tag and bump patch
    try:
        result = _sp.run(
            ["git", "tag", "--list", "v*", "--sort=-v:refname"],
            capture_output=True, text=True, cwd=ark_root,
        )
        tags = [t.strip() for t in result.stdout.strip().split("\n") if t.strip()]
    except Exception:
        tags = []

    if tags:
        latest = tags[0]  # e.g. v0.1.2
        parts = latest.lstrip("v").split(".")
        try:
            parts[-1] = str(int(parts[-1]) + 1)
            next_tag = "v" + ".".join(parts)
        except (ValueError, IndexError):
            next_tag = "v0.1.0"
    else:
        next_tag = "v0.1.0"

    tag = getattr(args, 'tag', None) or next_tag
    print(f"  {_c('Release version:', Colors.BOLD)} {tag}")

    # 2. Check for uncommitted changes
    status = _sp.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=ark_root)
    if status.stdout.strip():
        print(f"  {_c('Warning:', Colors.YELLOW)} You have uncommitted changes. Commit first or they won't be in the release.")
        if not prompt_yn("  Continue anyway?", default=False):
            return

    # 3. Create git tag
    r = _sp.run(["git", "tag", tag], capture_output=True, text=True, cwd=ark_root)
    if r.returncode != 0:
        if "already exists" in r.stderr:
            print(f"  Tag {tag} already exists, using it.")
        else:
            print(f"  {_c(f'Error creating tag: {r.stderr.strip()}', Colors.RED)}")
            return
    else:
        print(f"  {_c('Tagged:', Colors.GREEN)} {tag}")

    # 4. Create or update prod worktree
    prod_dir = _get_prod_worktree_dir()
    if not prod_dir.exists():
        print(f"  Creating prod worktree at {_c(str(prod_dir), Colors.CYAN)}...")
        r = _sp.run(
            ["git", "worktree", "add", "--detach", str(prod_dir), tag],
            capture_output=True, text=True, cwd=ark_root,
        )
        if r.returncode != 0:
            print(f"  {_c(f'Error: {r.stderr.strip()}', Colors.RED)}")
            return
    else:
        print(f"  Updating prod worktree to {_c(tag, Colors.BOLD)}...")
        # Use checkout -f to handle submodule/untracked file conflicts
        # that can silently prevent the worktree from advancing.
        r = _sp.run(
            ["git", "checkout", "-f", tag],
            capture_output=True, text=True, cwd=prod_dir,
        )
        if r.returncode != 0:
            print(f"  {_c(f'Error: {r.stderr.strip()}', Colors.RED)}")
            return
        # Clean leftover untracked files from previous releases
        _sp.run(
            ["git", "clean", "-fd"],
            capture_output=True, text=True, cwd=prod_dir,
        )

    print(f"  {_c('Prod worktree:', Colors.GREEN)} {prod_dir} → {tag}")

    # 5. Install in prod worktree using ark-prod conda env (non-editable)
    prod_python = _conda_env_python("ark-prod")
    print(f"  Installing dependencies in ark-prod (non-editable)...")
    r = _sp.run(
        [prod_python, "-m", "pip", "install", ".[webapp]", "-q"],
        capture_output=True, text=True, cwd=prod_dir,
    )
    if r.returncode != 0:
        print(f"  {_c(f'pip install warning: {r.stderr.strip()[:200]}', Colors.YELLOW)}")

    # 6. Symlink shared webapp.env into prod worktree
    prod_ark_dir = prod_dir / ".ark"
    prod_ark_dir.mkdir(parents=True, exist_ok=True)
    prod_env_link = prod_ark_dir / "webapp.env"
    main_env = get_config_dir() / "webapp.env"
    if main_env.exists():
        prod_env_link.unlink(missing_ok=True)
        prod_env_link.symlink_to(main_env)

    # 7. Restart prod service if running
    svc_path = _service_file_path(_PROD_SERVICE)
    if svc_path.exists():
        print(f"  Restarting prod service...")
        _sp.run(["systemctl", "--user", "restart", _PROD_SERVICE], check=True)
        print(f"  {_c('Prod service restarted.', Colors.GREEN)}")
    else:
        print(f"\n  Prod worktree ready. Install the service with:")
        print(f"    {_c('ark webapp install', Colors.BOLD)}")

    print(f"\n  {_c('Release complete!', Colors.GREEN + Colors.BOLD)} {tag}")
    print(f"  Prod: {_c(f'http://0.0.0.0:{_PROD_PORT}', Colors.CYAN)}")


def cmd_share(args):
    """Generate signed share links for a webapp project or user dashboard."""
    from ark import share as _share
    sub = getattr(args, 'share_cmd', None)
    try:
        if sub == 'create':
            if not getattr(args, 'project', None):
                print("Error: ark share create <project_id_or_name>", file=sys.stderr)
                return 1
            return _share.cmd_create(args.project, int(args.expires))
        if sub == 'user':
            if not getattr(args, 'email', None):
                print("Error: ark share user <email>", file=sys.stderr)
                return 1
            return _share.cmd_user(args.email, int(args.expires))
    except Exception as e:
        print(f"{_c('Error:', Colors.RED)} {e}", file=sys.stderr)
        return 1
    print(f"{_c('Unknown subcommand:', Colors.RED)} {sub}  (try: create | user)", file=sys.stderr)
    return 1


def cmd_access(args):
    """Manage the Cloudflare Access allowlist for idea2paper.org/dashboard."""
    from ark import access as _access
    sub = getattr(args, 'access_cmd', None)
    try:
        if sub == 'list' or sub is None:
            return _access.cmd_list()
        if sub == 'add':
            return _access.cmd_add(args.emails)
        if sub == 'remove':
            return _access.cmd_remove(args.emails)
        if sub == 'add-domain':
            return _access.cmd_add_domain(args.domains)
        if sub == 'remove-domain':
            return _access.cmd_remove_domain(args.domains)
    except RuntimeError as e:
        print(f"{_c('Error:', Colors.RED)} {e}", file=sys.stderr)
        return 1
    print(f"{_c('Unknown subcommand:', Colors.RED)} {sub}", file=sys.stderr)
    return 1


def cmd_webapp(args):
    """Start the ARK web app (lab-facing project submission portal)."""
    subcmd = getattr(args, 'webapp_cmd', None)
    if subcmd in ('disable', 'enable'):
        # Delegate to `ark web` logic
        args.web_cmd = subcmd
        args.project = None
        cmd_web(args)
        return

    dev = getattr(args, 'dev', False)

    if subcmd == 'install':
        _cmd_webapp_install(args.host, args.port, dev=dev)
        return
    if subcmd == 'uninstall':
        _cmd_webapp_uninstall(dev=dev)
        return
    if subcmd == 'status':
        _cmd_webapp_status(dev=dev)
        return
    if subcmd == 'logs':
        _cmd_webapp_logs(dev=dev, follow=getattr(args, 'follow', False),
                         lines=getattr(args, 'lines', 50))
        return
    if subcmd == 'restart':
        _cmd_webapp_restart(dev=dev)
        return
    if subcmd == 'release':
        _cmd_webapp_release(args)
        return

    try:
        import uvicorn
        from website.dashboard import create_app
        from website.dashboard.config import get_settings, _env_file
    except ImportError:
        print(f"{_c('Error:', Colors.RED)} Webapp dependencies not installed.")
        print(f"  Install with: {_c('pip install ark-research[webapp]', Colors.BOLD)}")
        print(f"  Or: {_c('pip install fastapi uvicorn[standard] sqlmodel httpx python-multipart itsdangerous', Colors.BOLD)}")
        sys.exit(1)

    host, port = args.host, args.port

    # Ensure config file exists
    settings = get_settings()
    if not (settings.smtp_user and settings.smtp_password):
        print(f"\n  {_c('Warning:', Colors.YELLOW)} SMTP not configured — magic link emails won't be sent.")
        print(f"  Edit {_c(str(_env_file()), Colors.CYAN)} and set SMTP_USER / SMTP_PASSWORD.\n")

    if args.daemon:
        print(f"\n  {_c('Deprecation:', Colors.YELLOW)} --daemon uses os.fork() and will be removed in a future release.")
        print(f"  Use {_c('ark webapp install', Colors.BOLD)} instead for a systemd user service.\n")
        _root = get_ark_root()
        _webapp_dir = _root / "ark_webapp"
        _webapp_dir.mkdir(exist_ok=True)
        pid_file = _webapp_dir / "webapp.pid"
        log_file = _webapp_dir / "webapp.log"
        pid = os.fork()
        if pid > 0:
            pid_file.write_text(str(pid))
            print(f"  {_c('ARK Webapp', Colors.BOLD)} started in background (PID {pid})")
            print(f"  URL: {_c(f'http://{host}:{port}', Colors.CYAN)}")
            print(f"  Log: {_c(str(log_file), Colors.DIM)}")
            return
        # Child process — detach and redirect file descriptors
        os.setsid()
        sys.stdin.close()
        fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(fd, 1)  # stdout
        os.dup2(fd, 2)  # stderr
        os.close(fd)

    print(f"\n  {_c('ARK Research Portal', Colors.BOLD)} — http://{host}:{port}\n")
    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


# ============================================================
#  ark web
# ============================================================

def cmd_web(args):
    """disable/enable webapp submissions + kill/re-queue jobs."""
    import sqlite3 as _sq
    import subprocess as _sp
    import shutil as _sh

    subcmd = args.web_cmd
    project_filter = getattr(args, 'project', None) or None

    _DISABLED_FLAG = get_ark_root() / "ark_webapp" / "disabled"
    _DB = get_config_dir() / "webapp.db"

    if not _DB.exists():
        print(f"{_c('Error:', Colors.RED)} No webapp DB found. Start the webapp first.")
        sys.exit(1)

    def _scancel(job_id):
        if job_id and job_id not in ("", "local"):
            _sp.run(["scancel", job_id], capture_output=True)

    def _submit(pid, mode, max_iter, user_id, con):
        """Try SLURM submit; fallback to 'local'. Returns new job_id."""
        if _sh.which("sbatch"):
            from website.dashboard.config import get_settings
            from website.dashboard.jobs import submit_job
            settings = get_settings()
            pdir = settings.projects_root / user_id / pid
            log_dir = pdir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            job_id = submit_job(pid, mode, max_iter, pdir, log_dir, settings)
        else:
            job_id = "local"
        con.execute(
            "UPDATE project SET status='queued', slurm_job_id=?, updated_at=datetime('now') WHERE id=?",
            (job_id, pid),
        )
        return job_id

    con = _sq.connect(str(_DB))

    # ── disable ──────────────────────────────────────────────
    if subcmd == "disable":
        _DISABLED_FLAG.touch()

        if project_filter:
            row = con.execute(
                "SELECT id, name, slurm_job_id FROM project WHERE name=? OR id=?",
                (project_filter, project_filter),
            ).fetchone()
            if not row:
                print(f"{_c('Error:', Colors.RED)} Project '{project_filter}' not found.")
                con.close(); sys.exit(1)
            pid, pname, job_id = row
            _scancel(job_id)
            con.execute(
                "UPDATE project SET status='stopped', updated_at=datetime('now') WHERE id=?",
                (pid,),
            )
            con.commit()
            print(f"  {_c('Stopped', Colors.RED)} '{pname}'")
            print(f"  Submissions gate: {_c('DISABLED', Colors.RED)}")
        else:
            rows = con.execute(
                "SELECT id, name, slurm_job_id FROM project "
                "WHERE status IN ('queued','running','pending')"
            ).fetchall()
            for pid, pname, job_id in rows:
                _scancel(job_id)
                con.execute(
                    "UPDATE project SET status='stopped', updated_at=datetime('now') WHERE id=?",
                    (pid,),
                )
            con.commit()
            print(f"  {_c('Webapp DISABLED', Colors.RED)} — stopped {len(rows)} active project(s)")
            print(f"  Re-enable with: {_c('ark web enable', Colors.BOLD)}")

    # ── enable ───────────────────────────────────────────────
    elif subcmd == "enable":
        _DISABLED_FLAG.unlink(missing_ok=True)

        if project_filter:
            row = con.execute(
                "SELECT id, name, mode, max_iterations, user_id, status FROM project "
                "WHERE name=? OR id=?",
                (project_filter, project_filter),
            ).fetchone()
            if not row:
                print(f"{_c('Error:', Colors.RED)} Project '{project_filter}' not found.")
                con.close(); sys.exit(1)
            pid, pname, mode, max_iter, user_id, status = row
            if status not in ("stopped", "failed", "done"):
                print(f"  '{pname}' has status '{status}' — nothing to re-queue.")
                con.close(); return
            try:
                job_id = _submit(pid, mode, max_iter, user_id, con)
                con.commit()
                print(f"  {_c('Queued', Colors.GREEN)} '{pname}' (job {job_id})")
            except Exception as e:
                print(f"  {_c('Submit failed:', Colors.RED)} {e}")
                con.close(); sys.exit(1)
            print(f"  Submissions gate: {_c('ENABLED', Colors.GREEN)}")
        else:
            active_count = con.execute(
                "SELECT COUNT(*) FROM project WHERE status IN ('queued','running')"
            ).fetchone()[0]

            print(f"  {_c('Webapp ENABLED', Colors.GREEN)}")

            if active_count:
                print(f"  {active_count} project(s) already active — queue advances automatically.")
            else:
                # Advance the oldest pending project
                pending = con.execute(
                    "SELECT id, name, mode, max_iterations, user_id FROM project "
                    "WHERE status='pending' ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
                if pending:
                    pid, pname, mode, max_iter, user_id = pending
                    try:
                        job_id = _submit(pid, mode, max_iter, user_id, con)
                        con.commit()
                        print(f"  Submitted next in queue: '{pname}' (job {job_id})")
                    except Exception as e:
                        print(f"  {_c('Submit failed:', Colors.RED)} {e}")
                else:
                    print(f"  No pending projects — queue is empty.")

    con.close()


# ============================================================
#  ark clear
# ============================================================

def _stop_project_if_running(project_dir: Path, name: str):
    """Stop a running project. Returns True if it was running."""
    running, pid = _is_running(project_dir)
    if not running:
        return False
    print(f"Stopping '{name}' (PID {pid})...")
    os.kill(pid, signal.SIGTERM)
    for _ in range(10):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    (project_dir / ".pid").unlink(missing_ok=True)
    return True


def cmd_clear(args):
    """Stop project and wipe runtime state/logs, keeping config, bot, and deep research."""
    name = args.project
    project_dir = get_projects_dir() / name

    if not project_dir.exists() or not (project_dir / "config.yaml").exists():
        print(f"{_c('Error:', Colors.RED)} Project '{name}' not found.")
        sys.exit(1)

    config = get_project_config(name)
    code_dir = config.get("code_dir", "")
    state_dir = Path(code_dir) / "auto_research" / "state" if code_dir else None
    logs_dir = Path(code_dir) / "auto_research" / "logs" if code_dir else None

    # Show what will be cleared
    print()
    print(f"  {_c(f'Clear: {name}', Colors.BOLD + Colors.CYAN)}")
    print()
    print(f"  {_c('Will remove:', Colors.YELLOW)}")
    print(f"    state/  (memory, scores, action plan, checkpoints, reviews, ...)")
    print(f"    logs/   (all run logs)")
    print(f"  {_c('Will keep:', Colors.DIM)}")
    print(f"    config.yaml, hooks.py, agents/, Telegram bot, deep_research.md")
    print()

    if not args.force:
        if not sys.stdin.isatty() or not prompt_yn(f"Clear runtime state for '{name}'?", default=False):
            print("Aborted.")
            return

    _stop_project_if_running(project_dir, name)

    cleared = []
    if state_dir and state_dir.exists():
        # Remove everything in state/ EXCEPT deep_research.md
        for f in state_dir.iterdir():
            if f.name != "deep_research.md":
                if f.is_dir():
                    shutil.rmtree(f)
                else:
                    f.unlink()
        cleared.append(str(state_dir))

    if logs_dir and logs_dir.exists():
        shutil.rmtree(logs_dir)
        logs_dir.mkdir()
        cleared.append(str(logs_dir))

    # Remove page images from latex dir
    latex_dir = Path(code_dir) / config.get("latex_dir", "paper") if code_dir else None
    if latex_dir and latex_dir.exists():
        for img in latex_dir.glob("page_*.png"):
            img.unlink()

    print(f"{_c('Cleared.', Colors.GREEN)} Project '{name}' will start fresh on next run.")
    print(f"  (deep_research.md preserved — use 'ark delete' to remove everything)")
    print()


# ============================================================
#  ark delete
# ============================================================

def cmd_delete(args):
    name = args.project
    projects_dir = get_projects_dir()
    project_dir = projects_dir / name

    if not project_dir.exists() or not (project_dir / "config.yaml").exists():
        print(f"{_c('Error:', Colors.RED)} Project '{name}' not found.")
        sys.exit(1)

    config = get_project_config(name)
    code_dir = config.get("code_dir", "")
    auto_research_dir = Path(code_dir) / "auto_research" if code_dir else None

    running, pid = _is_running(project_dir)
    if running:
        print(f"{_c('Warning:', Colors.YELLOW)} Project '{name}' is running (PID {pid}).")
        if not args.force:
            if not sys.stdin.isatty() or not prompt_yn("Stop and delete?", default=False):
                print("Aborted.")
                return

    print()
    print(f"  {_c('Will delete:', Colors.RED)}")
    print(f"    {project_dir}  (config, agents, hooks, bot setup)")
    if auto_research_dir and auto_research_dir.exists():
        print(f"    {auto_research_dir}  (state, logs, deep_research)")
    if code_dir:
        print(f"  {_c('Will NOT delete:', Colors.DIM)} {code_dir}  (your code)")

    if not args.force:
        print()
        if not sys.stdin.isatty() or not prompt_yn(f"Delete project '{name}'?", default=False):
            print("Aborted.")
            return

    _stop_project_if_running(project_dir, name)

    shutil.rmtree(project_dir)
    if auto_research_dir and auto_research_dir.exists():
        shutil.rmtree(auto_research_dir)

    print(f"{_c('Deleted.', Colors.GREEN)} Project '{name}' and all its data removed.")


# ============================================================
#  ark config
# ============================================================

def cmd_config(args):
    """View or edit project config."""
    name = args.project
    config = get_project_config(name)

    if not config:
        print(f"{_c('Error:', Colors.RED)} Project '{name}' not found.")
        sys.exit(1)

    config_file = get_projects_dir() / name / "config.yaml"

    # Show all config
    if not args.key:
        print()
        print(f"  {_c(f'Config for {name}', Colors.BOLD)}")
        print(f"  {'-' * 50}")
        for k, v in config.items():
            val_str = str(v)
            if len(val_str) > 80:
                val_str = val_str[:77] + "..."
            print(f"  {_c(k, Colors.CYAN):>40} = {val_str}")
        print()
        print(f"  {_c('File:', Colors.DIM)} {config_file}")
        print()
        return

    key = args.key

    # Get value
    if args.value is None:
        if key in config:
            print(f"  {key} = {config[key]}")
        else:
            print(f"  {_c('Key not found:', Colors.YELLOW)} {key}")
            print(f"  Available keys: {', '.join(config.keys())}")
        return

    # Set value (type coercion)
    value = args.value
    if value.lower() in ("true", "yes"):
        value = True
    elif value.lower() in ("false", "no"):
        value = False
    else:
        try:
            value = int(value)
        except ValueError:
            try:
                value = float(value)
            except ValueError:
                pass  # keep as string

    old = config.get(key, "(unset)")
    config[key] = value

    with open(config_file, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"  {_c(key, Colors.CYAN)} = {value}  {_c(f'(was: {old})', Colors.DIM)}")


# ============================================================
#  ark list
# ============================================================

def cmd_list(args):
    cmd_status(argparse.Namespace(project=None))


# ============================================================
#  ark cite-check / cite-search / cite-debug
# ============================================================

def cmd_cite_check(args):
    """Verify a project's references.bib against DBLP/CrossRef."""
    from ark.citation import verify_bib, fix_bib, cleanup_unused, parse_bib

    config = get_project_config(args.project)
    if not config:
        print(f"Project '{args.project}' not found.")
        sys.exit(1)

    code_dir = Path(config.get("code_dir", ""))
    latex_dir = code_dir / config.get("latex_dir", "Latex")
    bib_path = latex_dir / "references.bib"

    if not bib_path.exists():
        print(f"No references.bib found at {bib_path}")
        sys.exit(1)

    entries = parse_bib(str(bib_path))
    print(f"\n{styled(Style.BOLD, f'Citation Verification: {args.project}')}")
    print(f"  references.bib: {len(entries)} entries\n")

    results = verify_bib(str(bib_path))

    for r in results:
        if r.status == "VERIFIED":
            icon = styled(Style.GREEN, "  ✓")
            detail = styled(Style.DIM, r.details) if hasattr(Style, 'DIM') else r.details
            print(f"{icon} {r.entry_key:30s}  VERIFIED    {r.details}")
        elif r.status == "SINGLE_SOURCE":
            print(f"  ~ {r.entry_key:30s}  SINGLE_SRC  {r.details}")
        elif r.status == "CORRECTED":
            print(f"  {styled(Style.YELLOW, '↑')} {r.entry_key:30s}  CORRECTED   {r.details}")
        elif r.status == "NEEDS-CHECK":
            print(f"  {styled(Style.RED, '✗')} {r.entry_key:30s}  NEEDS-CHECK {r.details}")

    if args.fix:
        corrected = [r for r in results if r.status in ("CORRECTED", "SINGLE_SOURCE")]
        needs_check = [r for r in results if r.status == "NEEDS-CHECK"]
        if corrected or needs_check:
            fix_bib(str(bib_path), results)
            print(f"\n  Applied fixes: {len(corrected)} corrected, {len(needs_check)} tagged [NEEDS-CHECK]")

        removed = cleanup_unused(str(bib_path), str(latex_dir))
        if removed:
            print(f"  Removed {len(removed)} unused entries: {', '.join(removed)}")

    # Summary
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    parts = [f"{v} {k.lower()}" for k, v in counts.items()]
    print(f"\n  Summary: {', '.join(parts)}")


def cmd_cite_search(args):
    """Search academic databases for papers."""
    from ark.citation import search_papers, format_candidates_for_agent

    query = args.query
    print(f"\nSearching: \"{query}\"  (DBLP → CrossRef → arXiv → Semantic Scholar)\n")

    papers = search_papers(query, max_results=args.limit)

    if not papers:
        print("  No results found.")
        return

    print(format_candidates_for_agent(papers))
    print(f"  Total: {len(papers)} papers found")


def cmd_cite_debug(args):
    """Test citation pipeline using real Orchestrator code paths.

    Runs: Deep Research → citation bootstrap → writer (Related Work only)
          → citation verification → compile.
    Uses the actual Orchestrator methods so citation behavior is identical to ark run.
    """
    config = get_project_config(args.project)
    if not config:
        print(f"Project '{args.project}' not found.")
        sys.exit(1)

    project_dir = get_projects_dir() / args.project
    code_dir = Path(config.get("code_dir", ""))
    latex_dir = code_dir / config.get("latex_dir", "Latex")
    bib_path = latex_dir / "references.bib"
    lit_path = code_dir / "auto_research" / "state" / "literature.yaml"

    print(f"\n{styled(Style.BOLD, f'Citation Debug: {args.project}')}")
    print(f"  Title: {config.get('title', args.project)}")
    print(f"  Using real Orchestrator code paths\n")

    # Reset references.bib and literature.yaml for clean test
    bib_path.parent.mkdir(parents=True, exist_ok=True)
    bib_path.write_text("% ARK auto-managed references\n\n")
    if lit_path.exists():
        lit_path.unlink()
    print("  Reset references.bib and literature.yaml\n")

    # If --research, delete deep_research.md to force re-run
    if args.research:
        dr_file = code_dir / "auto_research" / "state" / "deep_research.md"
        if dr_file.exists():
            dr_file.unlink()
            print("  Deleted deep_research.md (will re-run Deep Research)\n")

    # Initialize real Orchestrator (for its methods, not full run)
    from ark.orchestrator import Orchestrator

    orch = Orchestrator(
        project=args.project,
        max_iterations=1,
        mode="paper",
        model=config.get("model", "claude"),
        code_dir=str(code_dir),
        project_dir=str(project_dir),
    )

    # Step 1: Deep Research + Citation Bootstrap (real code path)
    # _run_research_phase() calls _bootstrap_citations_from_deep_research() at the end
    print(styled(Style.BOLD, "═══ Step 1: Deep Research + Citation Bootstrap ═══\n"))
    if orch._should_run_research_phase():
        orch._run_research_phase()
    else:
        # Deep Research exists but maybe bootstrap hasn't run yet
        print("  Deep Research report already exists")
        orch._bootstrap_citations_from_deep_research()

    # Step 2: Writer (Related Work only)
    print(styled(Style.BOLD, "\n═══ Step 2: Writer (Related Work only) ═══\n"))
    latex_dir_name = config.get("latex_dir", "Latex")
    orch.run_agent("writer", f"""
Write the Related Work section in {latex_dir_name}/main.tex.
Use ONLY the citations available in {latex_dir_name}/references.bib (use \\cite{{key}} commands).
Read auto_research/state/literature.yaml for guidance on which papers to cite, their abstracts,
and where to place them. Do NOT modify references.bib.
Do NOT write other sections — only Related Work.
""", timeout=1200)

    # Step 3: Citation verification (real code path)
    print(styled(Style.BOLD, "\n═══ Step 3: Citation Verification ═══\n"))
    orch.compile_latex()
    orch._run_citation_verification()

    # Step 4: Final compile
    print(styled(Style.BOLD, "\n═══ Step 4: Final Compile ═══\n"))
    orch.compile_latex()

    # Summary
    from ark.citation import parse_bib
    if bib_path.exists():
        final_entries = parse_bib(str(bib_path))
        nc_count = sum(1 for e in final_entries if "NEEDS-CHECK" in str(e.get("fields", {}).get("note", "")))
        print(f"\n  {styled(Style.BOLD, 'Summary')}: {len(final_entries)} references in bib")
        if nc_count:
            print(f"  {nc_count} [NEEDS-CHECK] citation(s)")
    print(f"  Check: {latex_dir}/main.pdf\n")


# ============================================================
#  Main CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        prog="ark",
        description="ARK - Automatic Research Kit: AI-powered idea-to-paper automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              ark new mma                Create a new project
              ark run mma                Run in background
              ark run mma --mode paper   Run in paper mode
              ark status                 Show all projects
              ark status mma             Show project summary
              ark monitor mma            Live-monitor a project
              ark monitor mma -n 10      Monitor with 10s refresh
              ark update mma             Send an update
              ark config mma             Show project config
              ark config mma language zh Set a config value
              ark stop mma               Stop a running project
              ark delete mma             Delete a project
        """),
    )
    subparsers = parser.add_subparsers(dest="command")

    # ark new
    p_new = subparsers.add_parser("new", help="Create a new research project")
    p_new.add_argument("project", help="Project name")
    p_new.add_argument("--from-pdf", type=str, default=None,
                       help="Extract project spec from a PDF file")
    p_new.set_defaults(func=cmd_new)

    # ark create (alias)
    p_create = subparsers.add_parser("create", help="Create a new research project (alias for 'new')")
    p_create.add_argument("project", help="Project name")
    p_create.add_argument("--from-pdf", type=str, default=None,
                       help="Extract project spec from a PDF file")
    p_create.set_defaults(func=cmd_new)

    # ark run
    p_run = subparsers.add_parser("run", help="Run project orchestrator in the background")
    p_run.add_argument("project", help="Project name")
    p_run.add_argument("--mode", choices=["paper", "research", "dev"], default=None,
                       help="Mode: paper (review loop), research (experiment loop), or dev (development loop)")
    p_run.add_argument("--model", choices=["claude", "gemini", "codex"], default=None,
                       help="AI model backend")
    p_run.add_argument("--iterations", type=int, default=None, help="Number of iterations to run")
    p_run.add_argument("--max-days", type=float, default=None, help="Maximum runtime in days")
    p_run.add_argument("--no-research", action="store_true", default=False,
                       help="Skip Gemini Deep Research")
    p_run.set_defaults(func=cmd_run)

    # ark research
    p_research = subparsers.add_parser("research", help="Run Gemini Deep Research for a project")
    p_research.add_argument("project", help="Project name")
    p_research.add_argument("-q", "--query", help="Custom research query")
    p_research.add_argument("-f", "--force", action="store_true", help="Re-run even if report exists")
    p_research.set_defaults(func=cmd_research)

    # ark status
    p_status = subparsers.add_parser("status", help="Show project status summary")
    p_status.add_argument("project", nargs="?", default=None, help="Project name (omit for all)")
    p_status.set_defaults(func=cmd_status)

    # ark monitor
    p_monitor = subparsers.add_parser("monitor", help="Live-monitor a running project")
    p_monitor.add_argument("project", help="Project name")
    p_monitor.add_argument("-n", "--interval", type=int, default=5,
                           help="Refresh interval in seconds (default: 5)")
    p_monitor.set_defaults(func=cmd_monitor)

    # ark update
    p_update = subparsers.add_parser("update", help="Send updates to a running project")
    p_update.add_argument("project", help="Project name")
    p_update.add_argument("-m", "--message", help="Update message (non-interactive)")
    p_update.set_defaults(func=cmd_update)

    # ark stop
    p_stop = subparsers.add_parser("stop", help="Stop a running project")
    p_stop.add_argument("project", help="Project name")
    p_stop.set_defaults(func=cmd_stop)

    # ark clear
    p_clear = subparsers.add_parser("clear", help="Reset project state (keep config/bot, wipe state/logs)")
    p_clear.add_argument("project", help="Project name")
    p_clear.add_argument("-f", "--force", action="store_true", help="Skip confirmation")
    p_clear.set_defaults(func=cmd_clear)

    # ark delete
    p_delete = subparsers.add_parser("delete", help="Delete a project")
    p_delete.add_argument("project", help="Project name")
    p_delete.add_argument("-f", "--force", action="store_true",
                          help="Skip confirmation prompts")
    p_delete.set_defaults(func=cmd_delete)

    # ark config
    p_config = subparsers.add_parser("config", help="View or edit project config")
    p_config.add_argument("project", help="Project name")
    p_config.add_argument("key", nargs="?", default=None, help="Config key to view/set")
    p_config.add_argument("value", nargs="?", default=None, help="New value for the key")
    p_config.set_defaults(func=cmd_config)

    # ark list
    p_list = subparsers.add_parser("list", help="List all projects")
    p_list.set_defaults(func=cmd_list)

    p_setup_bot = subparsers.add_parser("setup-bot", help="Set up Telegram notifications (per-project or global)")
    p_setup_bot.add_argument("project", nargs="?", default=None, help="Project name for per-project setup (omit for global)")
    p_setup_bot.set_defaults(func=cmd_setup_bot)

    p_restart = subparsers.add_parser("restart", help="Restart a project (and Telegram bot daemon)")
    p_restart.add_argument("project", help="Project name")
    p_restart.set_defaults(func=cmd_restart)

    # ark webapp
    p_webapp = subparsers.add_parser("webapp", help="Start or manage lab-facing research portal")
    webapp_sub = p_webapp.add_subparsers(dest="webapp_cmd")
    webapp_sub.add_parser("disable", help="Block new project submissions")
    webapp_sub.add_parser("enable", help="Allow new project submissions")
    p_install = webapp_sub.add_parser("install", help="Install as systemd user service (auto-start on boot)")
    p_install.add_argument("--dev", action="store_true", help="Use dev environment (port 1027, shared DB)")
    p_uninstall = webapp_sub.add_parser("uninstall", help="Stop and remove systemd user service")
    p_uninstall.add_argument("--dev", action="store_true", help="Uninstall dev service")
    p_svc_status = webapp_sub.add_parser("status", help="Show systemd service status")
    p_svc_status.add_argument("--dev", action="store_true", help="Show dev service status")
    p_logs = webapp_sub.add_parser("logs", help="Show/follow service logs (journalctl)")
    p_logs.add_argument("--dev", action="store_true", help="Show dev service logs")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    p_logs.add_argument("-n", "--lines", type=int, default=50, help="Number of recent lines (default: 50)")
    p_svc_restart = webapp_sub.add_parser("restart", help="Restart the systemd service")
    p_svc_restart.add_argument("--dev", action="store_true", help="Restart dev service")
    p_release = webapp_sub.add_parser("release", help="Tag and deploy to prod environment")
    p_release.add_argument("--tag", type=str, default=None, help="Version tag (default: auto-increment)")
    p_webapp.add_argument("--port", type=int, default=9527, help="Port (default: 9527)")
    p_webapp.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    p_webapp.add_argument("--daemon", action="store_true", help="Run in background (deprecated, use 'install')")
    p_webapp.set_defaults(func=cmd_webapp)

    # ark share — generate read-only share links for a webapp project
    p_share = subparsers.add_parser(
        "share",
        help="Generate signed read-only share links for a webapp project",
    )
    share_sub = p_share.add_subparsers(dest="share_cmd")
    p_share_create = share_sub.add_parser("create", help="Generate a share URL for one project")
    p_share_create.add_argument("project", help="Project id or name (from webapp DB)")
    p_share_create.add_argument("--expires", default=90, type=int,
                                help="Link lifetime in days (default: 90)")
    p_share_user = share_sub.add_parser(
        "user",
        help="Generate a share URL for a user's dashboard (all their projects). Creates the user if absent.",
    )
    p_share_user.add_argument("email", help="User email (e.g. reviewer@idea2paper.org)")
    p_share_user.add_argument("--expires", default=90, type=int,
                              help="Link lifetime in days (default: 90)")
    p_share.set_defaults(func=cmd_share)

    # ark access — manage the Cloudflare Access allowlist for /dashboard
    p_access = subparsers.add_parser(
        "access",
        help="Manage CF Access allowlist for idea2paper.org/dashboard",
    )
    access_sub = p_access.add_subparsers(dest="access_cmd")
    access_sub.add_parser("list", help="Show current allowed emails and domains")
    p_acc_add = access_sub.add_parser("add", help="Add email(s) to allowlist")
    p_acc_add.add_argument("emails", nargs="+", help="Email address(es)")
    p_acc_rm = access_sub.add_parser("remove", help="Remove email(s) from allowlist")
    p_acc_rm.add_argument("emails", nargs="+", help="Email address(es)")
    p_acc_ad = access_sub.add_parser("add-domain", help="Add email domain (e.g. kaust.edu.sa)")
    p_acc_ad.add_argument("domains", nargs="+", help="Domain(s), with or without leading @")
    p_acc_rd = access_sub.add_parser("remove-domain", help="Remove email domain")
    p_acc_rd.add_argument("domains", nargs="+", help="Domain(s)")
    p_access.set_defaults(func=cmd_access)

    # ark cite-check
    p_cite_check = subparsers.add_parser("cite-check", help="Verify project citations against DBLP/CrossRef")
    p_cite_check.add_argument("project", help="Project name")
    p_cite_check.add_argument("--fix", action="store_true", help="Auto-fix correctable entries")
    p_cite_check.set_defaults(func=cmd_cite_check)

    # ark cite-search
    p_cite_search = subparsers.add_parser("cite-search", help="Search academic databases for papers")
    p_cite_search.add_argument("query", help="Search query")
    p_cite_search.add_argument("-n", "--limit", type=int, default=10, help="Max results (default: 10)")
    p_cite_search.set_defaults(func=cmd_cite_search)

    # ark cite-debug
    p_cite_debug = subparsers.add_parser("cite-debug", help="Two-round citation debug loop (internal)")
    p_cite_debug.add_argument("project", help="Project name")
    p_cite_debug.add_argument("--research", action="store_true", help="Re-run Deep Research before citation bootstrapping")
    p_cite_debug.set_defaults(func=cmd_cite_debug)

    # ark web disable/enable [project]
    p_web = subparsers.add_parser("web", help="Control webapp jobs and submission gate")
    web_sub = p_web.add_subparsers(dest="web_cmd")
    p_web_dis = web_sub.add_parser("disable", help="Close gate + stop all (or one) active job(s)")
    p_web_dis.add_argument("project", nargs="?", help="Project name/id to stop (omit = all)")
    p_web_ena = web_sub.add_parser("enable", help="Open gate + advance queue (or re-queue one project)")
    p_web_ena.add_argument("project", nargs="?", help="Project name/id to re-queue (omit = resume queue)")
    p_web.set_defaults(func=cmd_web)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
