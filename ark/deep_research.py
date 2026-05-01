#!/usr/bin/env python3
"""
Gemini Deep Research integration for ARK.

Uses Google's Gemini Deep Research agent to gather comprehensive background
research before starting the paper writing loop.
"""

import base64
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable


def get_gemini_api_key() -> str:
    """
    Return the Gemini API key from the process environment.

    ARK is multi-tenant: each user must supply their own key. There is
    no shared/global config fallback. The webapp injects the key from
    the project owner's encrypted user record into the orchestrator
    subprocess as an environment variable; CLI users must export
    ``GEMINI_API_KEY`` (or the synonym ``GOOGLE_API_KEY``) themselves.
    """
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


def build_research_query(config: dict) -> str:
    """Build a deep research query from project config."""
    title = config.get("title", "")
    venue = config.get("venue", "")
    venue_pages = config.get("venue_pages", "")
    goal_anchor = config.get("goal_anchor", "")
    research_idea = config.get("research_idea", "") or config.get("idea", "")

    query_parts = []

    if title:
        query_parts.append(
            f"I am writing an academic paper titled \"{title}\" "
            f"targeting {venue} conference."
        )
    elif venue:
        query_parts.append(
            f"I am writing an academic paper targeting {venue} conference."
        )

    if research_idea:
        query_parts.append(
            f"\nResearch idea:\n{research_idea}"
        )

    query_parts.append(
        "Please conduct comprehensive research on this topic. I need:"
    )
    query_parts.append(
        "1. A thorough literature review of the most relevant and recent papers "
        "(2022-2026), including key findings, methodologies, and gaps."
    )
    query_parts.append(
        "2. Current state-of-the-art approaches, benchmarks, and baselines."
    )
    query_parts.append(
        "3. Key technical challenges and open problems in this area."
    )
    query_parts.append(
        "4. Suggested experimental methodology and evaluation metrics."
    )
    query_parts.append(
        "5. Potential related work that should be cited and discussed."
    )
    query_parts.append(
        "6. External systems, platforms, tools, or frameworks that this research depends on. "
        "For each one, provide: what it is, its official website/repository, how to install "
        "it (pip, npm, conda, docker, etc.), and any known prerequisites or system requirements."
    )
    query_parts.append(
        "7. Concrete experimental methodology: what specific experiments should be run, "
        "what datasets or benchmarks to use, what metrics to measure, and what baselines to compare against."
    )
    query_parts.append(
        "8. Any API keys, credentials, accounts, or special access needed to run the experiments. "
        "Note which systems are free/open-source and which require paid access or registration."
    )

    if goal_anchor:
        # Extract the core contributions from goal anchor
        query_parts.append(
            f"\nOur paper's core focus:\n{goal_anchor}"
        )

    query_parts.append(
        "\nProvide the output as a well-structured research report in Markdown. "
        "Include a section titled '## Required Systems & Setup' that lists every external "
        "system, tool, or platform the project depends on, with official URLs, install commands, "
        "and verification steps. "
        "For every paper you reference, always include: the EXACT paper title as it appears on the publication, "
        "first author surname, year, and venue. "
        "Do NOT paraphrase or reconstruct paper titles from memory — use the exact title. "
        "Do NOT use only abbreviations (e.g. write 'Time-series Generative Adversarial Networks (TimeGAN) by Yoon et al., 2019, NeurIPS' "
        "instead of just 'TimeGAN'). This is critical for automated citation retrieval. "
        "Do NOT include BibTeX entries — they will be fetched separately."
    )

    return "\n".join(query_parts)


def _apply_url_annotations(text: str, annotations: Iterable | None) -> str:
    """Inline `[cite: N]` markers as markdown links using URLCitation data.

    The Deep Research API returns each citation marker as a substring
    range (start_index/end_index) plus a grounding URL on the parent
    TextContent. We rewrite ``text[start:end]`` from ``[cite: N]`` to
    ``[cite: N](url)`` in reverse offset order so earlier substitutions
    don't shift later indices.

    Annotations without a URL or with out-of-range offsets are skipped
    silently — the underlying body text already contains the marker, so
    a missing URL just means that citation stays unlinked rather than
    crashing the whole report.
    """
    if not annotations:
        return text
    spans: list[tuple[int, int, str]] = []
    for a in annotations:
        if getattr(a, "type", "") != "url_citation":
            continue
        start = getattr(a, "start_index", None)
        end = getattr(a, "end_index", None)
        url = getattr(a, "url", None)
        if start is None or end is None or not url:
            continue
        if start < 0 or end > len(text) or start >= end:
            continue
        spans.append((start, end, url))
    spans.sort(key=lambda s: s[0], reverse=True)
    for start, end, url in spans:
        marker = text[start:end]
        text = text[:start] + f"{marker}({url})" + text[end:]
    return text


def _assemble_report(outputs: Iterable, assets_dir: Path) -> str:
    """Build the final markdown body from a Deep Research interaction.

    Concatenates every TextContent in order (the bug we hit before was
    that we ``break``ed after the first text output, dropping ~80% of
    the report including the Sources list and the Gini-metrics
    section). ImageContent items are decoded from base64 and saved to
    ``assets_dir/figure_NN.png``, with a markdown image reference
    inserted where they appeared so the converter picks them up.

    Returns the joined markdown. Caller is responsible for prepending
    the header block (title, generated date, etc).
    """
    parts: list[str] = []
    fig_count = 0
    for o in outputs or []:
        otype = getattr(o, "type", "")
        if otype == "text":
            text = getattr(o, "text", "") or ""
            if not text.strip():
                continue
            text = _apply_url_annotations(text, getattr(o, "annotations", None))
            parts.append(text)
        elif otype == "image":
            data = getattr(o, "data", "") or ""
            if not data:
                continue
            mime = (getattr(o, "mime_type", "") or "image/png").lower()
            ext = ".png" if "png" in mime else (".jpg" if "jp" in mime else ".bin")
            fig_count += 1
            try:
                assets_dir.mkdir(parents=True, exist_ok=True)
                fig_path = assets_dir / f"figure_{fig_count:02d}{ext}"
                fig_path.write_bytes(base64.b64decode(data))
                # The markdown lives one level above assets_dir; reference
                # via the assets/ subfolder name so a relative-path
                # converter (weasyprint, pandoc) can resolve it.
                parts.append(f"\n![Figure {fig_count}]({assets_dir.name}/{fig_path.name})\n")
            except Exception as e:
                parts.append(f"\n*(failed to decode image #{fig_count}: {e})*\n")
        # Other content types (tool calls, code blocks API may add later)
        # are intentionally ignored — DR currently only emits text + image.
    return "\n\n".join(parts)


def run_deep_research(
    config: dict,
    output_dir: Path,
    custom_query: str = None,
    api_key: str = None,
) -> str:
    """Run Gemini Deep Research and save results.

    Args:
        config: Project config dict
        output_dir: Directory to save the research report
        custom_query: Deep research query (generated by initializer agent)
        api_key: Gemini API key (overrides stored key)

    Returns:
        Path to the saved research report, or empty string on failure
    """
    try:
        from google import genai
    except ImportError:
        print("Error: google-genai package not installed.")
        print("Install it with: pip install google-genai")
        return ""

    # Get API key
    key = api_key or get_gemini_api_key()
    if not key:
        print("Error: No Gemini API key found.")
        print("Set GEMINI_API_KEY env var or run 'ark new' to configure.")
        return ""

    # Use the provided query (generated by initializer agent)
    query = custom_query
    if not query:
        # Minimal fallback if no query provided
        title = config.get("title", "")
        idea = config.get("research_idea", "") or config.get("idea", "")
        venue = config.get("venue", "")
        query = (
            f"I am writing an academic paper titled \"{title}\" targeting {venue}.\n\n"
            f"Research idea:\n{idea[:4000]}\n\n"
            "Please conduct comprehensive research on this topic covering:\n"
            "1. Literature review of recent relevant papers (2022-2026)\n"
            "2. State-of-the-art approaches and baselines\n"
            "3. External systems and tools needed, with installation instructions\n"
            "4. Experimental methodology and evaluation metrics\n"
        )

    # Initialize client
    client = genai.Client(api_key=key)

    print()
    print("  Starting Gemini Deep Research...")
    print(f"  This may take 5-20 minutes. You can safely wait.")
    print()

    try:
        # Start deep research in background.
        # Switched 2026-04-29 from `deep-research-pro-preview-12-2025` after
        # repeated silent in_progress hangs (Google AI Developers Forum has
        # reports of the same recurring bug since 2026-03-31, where
        # interactions stay `in_progress` for 12+ hours and never complete).
        # The 04-2026 preview is built on Gemini 3.1 Pro and is the
        # speed-optimized variant; the Max sibling targets async/cron
        # workflows and is overkill for our interactive pipeline.
        interaction = client.interactions.create(
            input=query,
            agent="deep-research-preview-04-2026",
            background=True,
        )

        interaction_id = interaction.id
        print(f"  Research ID: {interaction_id}")
        print()

        # Poll for results
        start_time = time.time()
        last_status = ""
        spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        spin_idx = 0

        while True:
            interaction = client.interactions.get(interaction_id)
            status = interaction.status
            elapsed = int(time.time() - start_time)
            elapsed_str = f"{elapsed // 60}m {elapsed % 60}s"

            if status != last_status:
                print(f"  [{elapsed_str}] Status: {status}")
                last_status = status

            if status == "completed":
                # Assemble all outputs (text + images) into one report.
                # Earlier code took only outputs[0].text — that dropped
                # the Sources list and ~80% of the body. See
                # tests/unit/test_deep_research_assemble.py for the
                # interaction shape we expect.
                output_dir.mkdir(parents=True, exist_ok=True)
                assets_dir = output_dir / "deep_research_assets"
                report_text = _assemble_report(
                    list(interaction.outputs or []), assets_dir
                )

                if not report_text.strip():
                    print("  Warning: Research completed but no text output found.")
                    return ""

                report_path = output_dir / "deep_research.md"
                header = (
                    f"# Deep Research Report\n\n"
                    f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                    f"**Project**: {config.get('title', 'Unknown')}\n"
                    f"**Duration**: {elapsed_str}\n\n"
                    f"---\n\n"
                )

                report_path.write_text(header + report_text)

                print()
                print(f"  Research completed! ({elapsed_str})")
                print(f"  Report saved: {report_path}")
                print(f"  Outputs: {len(list(interaction.outputs or []))} items "
                      f"({sum(1 for o in (interaction.outputs or []) if getattr(o, 'type', '') == 'text')} text, "
                      f"{sum(1 for o in (interaction.outputs or []) if getattr(o, 'type', '') == 'image')} image)")
                print()

                return str(report_path)

            elif status in ("failed", "cancelled"):
                error_msg = ""
                if hasattr(interaction, "error") and interaction.error:
                    error_msg = str(interaction.error)
                print(f"  Research {status}: {error_msg}")
                return ""

            # Timeout after 60 minutes
            if elapsed > 3600:
                print("  Research timed out (60 minutes).")
                return ""

            # Show spinner
            spin_char = spinner[spin_idx % len(spinner)]
            print(f"\r  {spin_char} Researching... ({elapsed_str})", end="", flush=True)
            spin_idx += 1

            time.sleep(10)

    except Exception as e:
        print(f"  Deep Research error: {e}")
        return ""


def run_deep_research_async(
    config: dict,
    output_dir: Path,
    custom_query: str = None,
    api_key: str = None,
    on_complete: callable = None,
    on_error: callable = None,
) -> threading.Thread:
    """Run deep research in a background thread.

    Args:
        config, output_dir, custom_query, api_key: same as run_deep_research()
        on_complete: callback(report_path: str) called on success
        on_error: callback(error_msg: str) called on failure

    Returns:
        The started Thread object.
    """
    def _worker():
        try:
            result = run_deep_research(config, output_dir, custom_query, api_key)
            if result and on_complete:
                on_complete(result)
            elif not result and on_error:
                on_error("Deep Research returned no result")
        except Exception as e:
            if on_error:
                on_error(str(e))

    t = threading.Thread(target=_worker, daemon=True, name="deep-research")
    t.start()
    return t
