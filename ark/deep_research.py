#!/usr/bin/env python3
"""
Gemini Deep Research integration for ARK.

Uses Google's Gemini Deep Research agent to gather comprehensive background
research before starting the paper writing loop.
"""

import os
import threading
import time
from datetime import datetime
from pathlib import Path


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

    if goal_anchor:
        # Extract the core contributions from goal anchor
        query_parts.append(
            f"\nOur paper's core focus:\n{goal_anchor}"
        )

    query_parts.append(
        "\nProvide the output as a well-structured research report in Markdown. "
        "For every paper you reference, always include: the EXACT paper title as it appears on the publication, "
        "first author surname, year, and venue. "
        "Do NOT paraphrase or reconstruct paper titles from memory — use the exact title. "
        "Do NOT use only abbreviations (e.g. write 'Time-series Generative Adversarial Networks (TimeGAN) by Yoon et al., 2019, NeurIPS' "
        "instead of just 'TimeGAN'). This is critical for automated citation retrieval. "
        "Do NOT include BibTeX entries — they will be fetched separately."
    )

    return "\n".join(query_parts)


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
        custom_query: Custom research query (overrides auto-generated)
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

    # Build query
    query = custom_query or build_research_query(config)

    # Initialize client
    client = genai.Client(api_key=key)

    print()
    print("  Starting Gemini Deep Research...")
    print(f"  This may take 5-20 minutes. You can safely wait.")
    print()

    try:
        # Start deep research in background
        interaction = client.interactions.create(
            input=query,
            agent="deep-research-pro-preview-12-2025",
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
                # Extract the final text output
                report_text = ""
                for output in interaction.outputs:
                    if hasattr(output, "text") and output.text:
                        report_text = output.text
                        break

                if not report_text:
                    print("  Warning: Research completed but no text output found.")
                    return ""

                # Save report
                output_dir.mkdir(parents=True, exist_ok=True)
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
