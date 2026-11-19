#!/usr/bin/env python3
"""
Gemini Deep Research integration for ARK.

Uses Google's Gemini Deep Research agent to gather comprehensive background
research before starting the paper writing loop.
"""

import base64
from bisect import bisect_right
import hashlib
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

# Silence google-genai's repeated "Both GOOGLE_API_KEY and GEMINI_API_KEY are
# set. Using GOOGLE_API_KEY." notice. ARK deliberately sets BOTH to the SAME key
# (GEMINI_API_KEY for litellm/OpenHands, GOOGLE_API_KEY for the genai SDK and
# PaperBanana) and passes the key explicitly to genai.Client, so this is pure
# noise. Filter only that one message; keep every other genai log intact. The
# logger is process-global, so installing the filter here covers all genai use.
import logging as _logging
_logging.getLogger("google_genai._api_client").addFilter(
    lambda _r: "Both GOOGLE_API_KEY and GEMINI_API_KEY" not in _r.getMessage()
)


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
        "1. The original work establishing the problem, the closest competing methods and "
        "the strongest recent comparisons, including findings, assumptions and gaps."
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


def _field(item, name, default=None):
    # SDKs predating the steps schema retain newer response fields as raw JSON.
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _linked_text_blocks(outputs: list) -> dict[int, str]:
    """Resolve local and report-wide byte offsets before inserting image references."""
    blocks = {}
    offsets = []
    indices = []
    annotations = []
    length = 0
    for index, item in enumerate(outputs):
        if _field(item, "type") != "text":
            continue
        data = (_field(item, "text") or "").encode()
        blocks[index] = data
        indices.append(index)
        offsets.append(length)
        length += len(data)
        annotations.extend((index, a) for a in _field(item, "annotations") or [])

    links = {}
    for index, annotation in annotations:
        start = _field(annotation, "start_index")
        end = _field(annotation, "end_index")
        url = _field(annotation, "url")
        if (_field(annotation, "type") != "url_citation" or not url
                or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end):
            continue
        # Some responses attach ALL annotations to the first text item and
        # index into the concatenated text (no joins or image placeholders).
        if not blocks[index][start:end].startswith(b"[cite:"):
            location = bisect_right(offsets, start) - 1
            other = indices[location]
            local_start, local_end = start - offsets[location], end - offsets[location]
            if (local_end <= len(blocks[other])
                    and blocks[other][local_start:local_end].startswith(b"[cite:")):
                index, start, end = other, local_start, local_end
        if end <= len(blocks[index]):
            urls = links.setdefault((index, end), [])
            if url not in urls:
                urls.append(url)

    for (index, end), urls in sorted(links.items(), reverse=True):
        # Several sources can ground the same citation marker.
        suffix = f"({urls[0]})" + "".join(f" [source]({url})" for url in urls[1:])
        data = blocks[index]
        blocks[index] = data[:end] + suffix.encode() + data[end:]
    return {index: data.decode("utf-8", errors="replace") for index, data in blocks.items()}


def _apply_url_annotations(text: str, annotations: Iterable | None) -> str:
    return _linked_text_blocks([{"type": "text", "text": text, "annotations": annotations}])[0]


def _model_output_contents(interaction) -> list:
    """Flatten an interaction's results into a list of content items.

    The May-2026 API replaced the flat ``outputs`` array with a ``steps``
    array (google-genai >= 2.0): model content now lives in
    ``steps[].content`` on steps with ``type == "model_output"`` (other step
    types — ``thought``, tool calls — carry no report content). Items keep
    the same shape (.type/.text/.annotations/.data/.mime_type), so the rest
    of this module is schema-agnostic. Falls back to ``outputs`` for the
    legacy schema (old SDKs / recorded fixtures).
    """
    steps = _field(interaction, "steps")
    if steps is None:
        return list(_field(interaction, "outputs") or [])
    items: list = []
    for s in steps or []:
        if _field(s, "type") != "model_output":
            continue
        items.extend(list(_field(s, "content") or []))
    return items


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
    outputs = list(outputs or [])
    linked = _linked_text_blocks(outputs)
    for index, o in enumerate(outputs):
        otype = _field(o, "type")
        if otype == "text":
            text = linked[index]
            if not text.strip():
                continue
            parts.append(text)
        elif otype == "image":
            data = _field(o, "data") or ""
            if not data:
                continue
            mime = (_field(o, "mime_type") or "image/png").lower()
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


def research_report_status(state_dir: Path) -> str:
    """Classify the saved literature survey, including pre-receipt projects."""
    report = state_dir / "deep_research.md"
    degraded = (state_dir / "research_degraded.txt").is_file()
    data = report.read_bytes() if report.is_file() else b""
    if not data.strip():
        return "degraded" if degraded else "missing"
    if data.startswith(b"# Literature Survey (fallback)"):
        return "degraded"
    receipt_path = state_dir / "deep_research_receipt.json"
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text())
            if (isinstance(receipt, dict) and receipt.get("provider") == "google"
                    and receipt.get("status") == "completed" and receipt.get("interaction_id")
                    and receipt.get("report_sha256") == hashlib.sha256(data).hexdigest()):
                return "verified"
        except (OSError, ValueError):
            pass
        return "invalid_receipt"
    header, _, body = data.partition(b"\n---\n")
    if (header.startswith(b"# Deep Research Report\n") and b"**Duration**:" in header
            and b"**Generated**:" in header and body.strip()):
        return "legacy"
    return "degraded" if degraded else "available"


def run_deep_research(
    config: dict,
    output_dir: Path,
    custom_query: str = None,
    api_key: str = None,
    interaction_id: str | None = None,
) -> str:
    """Run Gemini Deep Research and save results.

    Args:
        config: Project config dict
        output_dir: Directory to save the research report
        custom_query: Deep research query (generated by initializer agent)
        api_key: Gemini API key (overrides stored key)
        interaction_id: Resume a known Google interaction without creating a new one.

    Returns:
        Path to the saved research report, or empty string on failure
    """
    import fcntl

    output_dir.mkdir(parents=True, exist_ok=True)
    # Continue and a worker finishing its last poll may overlap. Serialize
    # the whole claim/publish transaction: waiters reuse the completed report.
    with (output_dir / ".deep_research.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if research_report_status(output_dir) in ("verified", "legacy", "available"):
            (output_dir / "research_degraded.txt").unlink(missing_ok=True)
            return str(output_dir / "deep_research.md")
        return _run_google_interaction(config, output_dir, custom_query, api_key, interaction_id)


def _run_google_interaction(config, output_dir, custom_query, api_key, interaction_id):
    from ark.orchestrator.state import _atomic_write_text

    report_path = output_dir / "deep_research.md"
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
            "1. Foundational sources, closest prior work and relevant recent comparisons\n"
            "2. State-of-the-art approaches and baselines\n"
            "3. External systems and tools needed, with installation instructions\n"
            "4. Experimental methodology and evaluation metrics\n"
        )

    # Ask for checkable primary evidence even when the initializer supplied the
    # topic. The writer needs what a paper supports, not just a list of titles.
    query += (
        "\n\nFor each directly relevant source, identify the original publication or "
        "dataset, its DOI or official URL, and the section, table or result supporting "
        "the claim. Distinguish the source's finding from its applicability to this "
        "project. Include relevant foundational work as well as recent comparisons. "
        "Identify the methods or metrics in the experimental comparison and trace them "
        "to the relevant original papers. Treat job scheduling, art generation and manuscript "
        "delivery as workflow instructions unless they are themselves the research subject. "
        "Flag inaccessible originals and unresolved claims."
    )
    client = genai.Client(api_key=key)
    pending = output_dir / "deep_research_interaction.json"

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
        agent = "deep-research-preview-04-2026"
        # The initializer's suggested query may be unavailable on Continue.
        # The saved idea is stable across process restarts of the same project.
        request_hash = hashlib.sha256(query.encode()).hexdigest()
        idea = output_dir / "idea.md"
        idea_hash = hashlib.sha256(idea.read_bytes()).hexdigest() if idea.is_file() else None
        if not interaction_id and pending.is_file():
            saved = json.loads(pending.read_text())
            if saved.get("agent") == agent and (saved.get("query_sha256") == request_hash
                    or (idea_hash and saved.get("idea_sha256") == idea_hash)):
                interaction_id = saved["interaction_id"]
        if not interaction_id:
            interaction = client.interactions.create(input=query, agent=agent, background=True)
            interaction_id = interaction.id
        _atomic_write_text(pending, json.dumps({
            "agent": agent, "interaction_id": interaction_id, "query_sha256": request_hash,
            "idea_sha256": idea_hash,
        }) + "\n")
        print(f"  Research ID: {interaction_id}")
        print()

        # Poll for results
        start_time = time.time()
        last_status = ""
        spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        spin_idx = 0

        while True:
            interaction = client.interactions.get(interaction_id)
            status = _field(interaction, "status")
            elapsed = int(time.time() - start_time)
            elapsed_str = f"{elapsed // 60}m {elapsed % 60}s"

            if status != last_status:
                print(f"  [{elapsed_str}] Status: {status}")
                last_status = status

            if status == "completed":
                agent = _field(interaction, "agent") or agent
                # Assemble all outputs (text + images) into one report.
                # Earlier code took only outputs[0].text — that dropped
                # the Sources list and ~80% of the body. See
                # tests/unit/test_deep_research_assemble.py for the
                # interaction shape we expect.
                output_dir.mkdir(parents=True, exist_ok=True)
                assets_dir = output_dir / "deep_research_assets"
                contents = _model_output_contents(interaction)
                report_text = _assemble_report(contents, assets_dir)

                if not report_text.strip():
                    print("  Warning: Research completed but no text output found.")
                    return ""

                header = (
                    f"# Deep Research Report\n\n"
                    f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                    f"**Project**: {config.get('title', 'Unknown')}\n"
                    f"**Provider**: Google Gemini\n"
                    f"**Agent**: {agent}\n"
                    f"**Interaction ID**: {interaction_id}\n"
                    f"**Duration**: {elapsed_str}\n\n"
                    f"---\n\n"
                )

                document = header + report_text
                usage = _field(interaction, "usage")
                if hasattr(usage, "model_dump"):
                    usage = usage.model_dump(mode="json", exclude_none=True)
                receipt = {
                    "provider": "google", "agent": agent, "interaction_id": interaction_id,
                    "status": status, "report_sha256": hashlib.sha256(document.encode()).hexdigest(),
                    "usage": usage if isinstance(usage, dict) else None,
                }
                # The report is the stage's completion marker. Publish it after its
                # receipt so a reader cannot mistake an unfinished write for success.
                _atomic_write_text(output_dir / "deep_research_receipt.json", json.dumps(receipt, indent=2) + "\n")
                _atomic_write_text(report_path, document)
                (output_dir / "research_degraded.txt").unlink(missing_ok=True)
                pending.unlink(missing_ok=True)

                print()
                print(f"  Research completed! ({elapsed_str})")
                print(f"  Report saved: {report_path}")
                print(f"  Outputs: {len(contents)} items "
                      f"({sum(1 for o in contents if _field(o, 'type') == 'text')} text, "
                      f"{sum(1 for o in contents if _field(o, 'type') == 'image')} image)")
                print()

                return str(report_path)

            elif status in ("failed", "cancelled"):
                error_msg = ""
                if hasattr(interaction, "error") and interaction.error:
                    error_msg = str(interaction.error)
                print(f"  Research {status}: {error_msg}")
                pending.unlink(missing_ok=True)
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
        if interaction_id and getattr(e, "status_code", None) == 404:
            pending.unlink(missing_ok=True)
        print(f"  Deep Research error: {e}")
        return ""


_DR_ATTEMPTS = 3
_DR_BACKOFF_SECONDS = 20


def _parse_openrouter_body(text: str, elapsed_str: str) -> tuple:
    """Turn an OpenRouter response body into (data, reason).

    ``data`` is the parsed JSON, or None with a human reason. Two shapes that
    are NOT plain JSON have bitten us, both while the status line already said
    200 (so the gateway could no longer signal an error):

    * whitespace-only — OpenRouter pads a slow generation to hold the
      connection open, and when the upstream model dies the body ends with
      nothing but that padding. Observed 2026-08-06: 974 lines / ~5.4 KB of
      padding, which `resp.json()` reported as the baffling
      "Expecting value: line 975 column 1".
    * SSE comment lines (": OPENROUTER PROCESSING") mixed in, which are
      comments per the SSE spec and must simply be skipped.

    Neither is a bug in our request, so both are worth retrying rather than
    surfacing as a JSON traceback.
    """
    import json

    if not text or not text.strip():
        return None, (f"provider returned an empty body after {elapsed_str} "
                      f"(HTTP 200 with only keep-alive padding — the upstream "
                      f"research model did not finish)")
    # Drop SSE comments/blank lines, keep everything else.
    payload = "\n".join(
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith(":")
    ).strip()
    if not payload:
        return None, (f"provider returned only keep-alive comments after "
                      f"{elapsed_str} — no report was generated")
    # A streamed body arrives as `data: {...}` events; take the last complete one.
    if payload.startswith("data:"):
        events = [ln[5:].strip() for ln in payload.splitlines()
                  if ln.startswith("data:") and ln[5:].strip() not in ("", "[DONE]")]
        payload = events[-1] if events else ""
        if not payload:
            return None, f"stream carried no data events after {elapsed_str}"
    try:
        return json.loads(payload), ""
    except Exception as e:  # noqa: BLE001
        return None, f"response was not JSON after {elapsed_str}: {e}"


def run_deep_research_openrouter(
    config: dict,
    output_dir: Path,
    custom_query: str = None,
    api_key: str = None,
    model: str = "perplexity/sonar-deep-research",
    use_web_plugin: bool = True,
) -> str:
    """Run Deep Research via OpenRouter (Perplexity sonar-deep-research).

    Unlike Gemini's Interactions API, this is a plain OpenAI-compatible chat
    completion, so it runs fully through OpenRouter (one key, cost tracked) and
    returns titled citations in ``message.annotations`` — which we render into a
    ``## Sources`` section that the citation bootstrap can actually use (Gemini
    DR only returned bare domains, no paper titles). The OpenRouter ``web``
    plugin is enabled by default to force a live search + citations (the
    deep-research model occasionally skips searching otherwise).

    Returns the path to ``deep_research.md`` or "" on failure.
    """
    key = api_key or os.environ.get("OPENROUTER_API_KEY", "") or config.get("openrouter_api_key", "")
    if not key:
        print("Error: No OpenRouter API key found for Deep Research.")
        return ""

    query = custom_query or build_research_query(config)
    body = {"model": model, "messages": [{"role": "user", "content": query}]}
    if use_web_plugin:
        body["plugins"] = [{"id": "web", "max_results": 8}]

    import httpx
    print(f"  Starting OpenRouter Deep Research ({model})...")
    print("  This may take 2-10 minutes. You can safely wait.")

    data = None
    for attempt in range(1, _DR_ATTEMPTS + 1):
        start = time.time()
        try:
            resp = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=body, timeout=900,
            )
        except Exception as e:  # noqa: BLE001
            reason = f"request failed: {e}"
            resp = None
        else:
            elapsed = int(time.time() - start)
            elapsed_str = f"{elapsed // 60}m {elapsed % 60}s"
            if resp.status_code != 200:
                reason = f"HTTP {resp.status_code}: {resp.text[:200]}"
            else:
                data, reason = _parse_openrouter_body(resp.text, elapsed_str)
                if data is not None:
                    break
        print(f"  Deep Research attempt {attempt}/{_DR_ATTEMPTS} failed — {reason}")
        if attempt < _DR_ATTEMPTS:
            backoff = _DR_BACKOFF_SECONDS * attempt
            print(f"  Retrying in {backoff}s...")
            time.sleep(backoff)

    if data is None:
        print("  Deep Research failed after "
              f"{_DR_ATTEMPTS} attempts — no report was produced.")
        return ""

    try:
        msg = data["choices"][0]["message"]
    except Exception as e:  # noqa: BLE001
        print(f"  Deep Research: unexpected response shape: {e}")
        return ""

    report_text = (msg.get("content") or "").strip()
    if not report_text:
        print("  Warning: Deep Research returned empty content.")
        return ""

    # Collect titled citations from message.annotations (url_citation), dedup.
    seen, sources = set(), []
    for a in (msg.get("annotations") or []):
        if isinstance(a, dict) and a.get("type") == "url_citation":
            uc = a.get("url_citation", {}) or {}
            title = (uc.get("title") or "").strip()
            url = (uc.get("url") or "").strip()
            k = (title, url)
            if (title or url) and k not in seen:
                seen.add(k)
                sources.append((title, url))

    sources_md = ""
    if sources:
        sources_md = "\n\n## Sources\n\n" + "\n".join(
            f"{i}. {title or '(untitled)'}" + (f" — {url}" if url else "")
            for i, (title, url) in enumerate(sources, 1)
        )

    cost = (data.get("usage") or {}).get("cost")
    cost_str = f" · cost ${cost:.3f}" if isinstance(cost, (int, float)) else ""
    # Surface the provider-billed cost to the pipeline ledger: DR was the
    # single most expensive call of a run yet appeared in NO cost report —
    # a real user's dashboard showed $5 against a $20 OpenRouter bill.
    if isinstance(cost, (int, float)):
        try:
            (output_dir / "deep_research_cost.usd").write_text(f"{float(cost):.6f}")
        except Exception:
            pass
    header = (
        f"# Deep Research Report\n\n"
        f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"**Project**: {config.get('title', 'Unknown')}\n"
        f"**Backend**: OpenRouter / {model}\n"
        f"**Duration**: {elapsed_str}{cost_str}\n\n"
        f"---\n\n"
    )
    report_path = output_dir / "deep_research.md"
    report_path.write_text(header + report_text + sources_md)
    print(f"  Research completed! ({elapsed_str}, {len(sources)} sources)")
    print(f"  Report saved: {report_path}")
    return str(report_path)


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
