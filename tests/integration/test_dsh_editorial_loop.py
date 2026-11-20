"""Offline qualification of the DSH owner and blind reviewer against the real editorial domain.

Requires two installed test inputs: IDEA2PAPER_TEST_DSH (CLI) and IDEA2PAPER_TEST_PROFILE
(base-backed profile). Run with python -m pytest tests/integration/test_dsh_editorial_loop.py.
Model responses and the bibliography index are local fixtures. A completed measurement is imported.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import runpy
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fitz
import yaml

from ark.orchestrator import native_domain as domain

ROOT = Path(__file__).resolve().parents[2]
BIB = "@article{method, title={Counting observed events}, author={Sample, Ann}, journal={Fixture}, year={2020}}\n"
EXTRA = "@article{extra, title={Independent sequences}, author={Sample, Ben}, journal={Fixture}, year={2021}}\n"
PASSAGE = "The count includes only events recorded by the observer."
SOURCE_URL = "https://example.org/fixture/counting-observed-events"
SOURCE = "sources/" + hashlib.sha256(SOURCE_URL.encode()).hexdigest()[:24] + ".txt"
CLAIM = "The observed count follows the event definition"
GOOD = CLAIM + r"~\cite{method}."
BAD = CLAIM + r".\nocite{*}\par\noindent\mbox{" + "W" * 90 + "}"
PAPER = r"""\documentclass{article}
\usepackage{graphicx,hyperref}
\begin{document}
\section{Observations}
A fixed stream records the observed events. CLAIM
\begin{figure}[h]\centering\includegraphics[width=11cm]{figures/counts.png}
\caption{Counts for the observed stream.}\end{figure}
\paragraph{Observation unit.} Each input is a stream of distinct symbols in the
order in which they arrived. The recorded value is the number of arrivals.
Reordering an input has no effect on its total. Dropping an arrival does.
\paragraph{Limits.} These observations concern the named streams. There is no
random sampling protocol and there are no independent replications. The bars
show the observations individually so readers can see their relative size.
\paragraph{Interpretation.} The difference describes the supplied inputs.
It does not estimate a population effect. A new input could have any length
and would need its own observation before it could be compared.
\paragraph{Data retention.} Save the stream lengths in acquisition order.
Retaining both totals keeps the plotted ordering independently checkable.
\section*{Acknowledgements}
The manuscript was prepared using idea2paper.org.
\clearpage
\bibliographystyle{plain}
\bibliography{references}
\appendix
\section{Replication protocol}
Read the stream in order and count each recorded event once. The bars display
the recorded totals for each input.

Start with an empty counter for the first stream. Increment the counter once when
reading each symbol and record the value after the last symbol. Repeat with a newly
initialized counter for the next stream. Never carry the accumulator across inputs.
Collect the end values in a list, making the original stream ordering explicit.
Use equal widths for both bars and a common origin so that their relative heights
represent counts on a single scale. To check the figure, compare each bar against
the element at the corresponding position in the saved count list.
\end{document}
""".replace("CLAIM", GOOD)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(root):
    from PIL import Image, ImageDraw

    root.mkdir()
    (root / "config.yaml").write_text(yaml.safe_dump(dict(idea="Count a fixed event stream",
        venue_format="article", venue_pages=1, layout_mode="strict", skip_deep_research=True,
        figure_generation="none")))
    state = root / "auto_research/state"
    state.mkdir(parents=True)
    (state / "ethical_review.json").write_text(json.dumps({"verdict": "proceed"}))
    project = domain.NativeProject(root)
    project.invoke("project_status", {})
    (project.latex_dir / "main.tex").write_text(PAPER)
    (project.latex_dir / "references.bib").write_text(BIB)
    # Import a tiny completed measurement and draw its actual values. The test starts at writing.
    measured = root / "results/count.json"
    measured.parent.mkdir(exist_ok=True)
    counts = [len(stream) for stream in ("abc", "abcde")]
    measured.write_text(json.dumps({"counts": counts}))
    plot = project.figures_dir / "counts.png"
    img = Image.new("RGB", (120, 80), "white")
    draw = ImageDraw.Draw(img)
    for i, value in enumerate(json.loads(measured.read_text())["counts"]):
        draw.rectangle((20 + i * 50, 70 - 10 * value, 45 + i * 50, 70), fill="navy")
    img.save(plot)
    project.state.update(experiment={"artifact": "results/count.json", "status": "completed",
                                      "sha256": digest(measured)}, plots={"counts.png": {
        "status": "completed", "input_access": "worker_observed_read", "input_sha256": digest(measured),
        "artifact": "paper/figures/counts.png", "sha256": digest(plot)}})
    project.save()
    return project


# The publisher registry is the only external scientific service in this replay.
def offline_bib(path):
    return [SimpleNamespace(entry_key=e["key"], status="VERIFIED", details="Fixture index")
            for e in domain.citation.parse_bib(path) if e["key"] in {"method", "extra"}]


def offline_source(entry):
    if entry["key"] == "method":
        return {"url": SOURCE_URL, "source": "Indexed abstract", "title": "Counting observed events",
                "passage": PASSAGE}


def stream(model, message):
    ident = uuid.uuid4().hex
    tool = isinstance(message, tuple)
    start = ({"type": "tool_use", "id": ident, "name": message[0], "input": {}}
             if tool else {"type": "text", "text": ""})
    delta = ({"type": "input_json_delta", "partial_json": json.dumps(message[1])}
             if tool else {"type": "text_delta", "text": message})
    events = [
        {"type": "message_start", "message": {"id": ident, "model": model, "usage": {"input_tokens": 10, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": start},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn"}, "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def review_reply(body):
    texts = [part.get("text", "") for msg in body["messages"]
             for part in (msg["content"] if isinstance(msg["content"], list) else [])]
    history = "\n".join(texts)
    assert "private-owner-canary" not in history
    match = re.search(r'"revision"\s*:\s*"([a-f0-9]{12,64})"', history)
    assert match, history[:2000]
    if not any(part.get("name") == "read_evidence" and part["input"].get("artifact") == "paper/main.pdf"
               for msg in body["messages"] if isinstance(msg["content"], list) for part in msg["content"]):
        return "read_evidence", {"artifact": "paper/main.pdf", "page": 1}
    def check(ref):
        return {"status": "assessed", "reason": "Checked the recorded observation.", "evidence": [ref]}
    return json.dumps({"revision": match[1], "score": 8, "verdict": "accept", "issues": [],
        "summary": "A small reproducible count.", "alternative": "A different stream may yield different counts.",
        "strengths": ["The bars can be checked against the original counts."], "checks": {
        "manuscript": check({"artifact": "paper/main.pdf", "page": 1, "quote": "A fixed stream records the observed events."}),
        "visual": check({"artifact": "paper/main.pdf", "page": 1, "visual": True}),
        "experiment": check({"artifact": "results/count.json", "quote": '{"counts": [3, 5]}'}),
        "citations": check({"artifact": SOURCE, "quote": PASSAGE})}})


def editorial_revision(installed_profile, dsh, tmp_path):
    assert shutil.which("pdflatex") and shutil.which("bibtex")
    root = tmp_path / "project"
    project = prepare(root)
    state = project.state_dir
    home = tmp_path / "dsh"
    profile = home / "profiles/idea2paper"
    shutil.copytree(installed_profile, profile, symlinks=True)
    def edit(before, after):
        return "write_project_file", {"path": "paper/main.tex", "replace": before, "content": after}
    compile_and_verify = [("verify_citations", {}), ("compile_paper", {})]
    request = ("request_review", {})
    deliver = ("deliver", {})
    owner_steps = [
        *compile_and_verify, request, deliver, "Original manuscript delivered.",
        edit(GOOD, BAD), *compile_and_verify, request, deliver,
        ("write_project_file", {"path": "paper/references.bib", "content": BIB + EXTRA}),
        *compile_and_verify, request, deliver,
        edit(BAD, GOOD + " We use the same definition throughout."),
        *compile_and_verify, deliver, request, deliver, "Revised manuscript delivered.",
    ]
    operations, errors, rendered = [], [], []

    def owner_reply(body):
        reply = owner_steps.pop(0)
        feedback = json.dumps(body["messages"][-1]["content"])
        if isinstance(reply, tuple) and reply[0] == "write_project_file":
            args = reply[1]
            if args["path"] == "paper/references.bib" or args.get("replace") == BAD:
                assert "column_overflow" in feedback and "Verify the sources" in feedback
        if reply == request and owner_steps[0] == deliver and len(owner_steps) == 2:
            assert "Independent review is missing, stale, or requires revision" in feedback
        return reply

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                tools = {tool["name"] for tool in body["tools"]}
                reply = review_reply(body) if "read_evidence" in tools else owner_reply(body)
                if isinstance(reply, tuple):
                    assert reply[0] in tools, (reply, tools)
                data = stream(body["model"], reply)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                errors.append(exc)
                self.send_error(400, "Scripted local model rejected request")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    (profile / "cordis.patch.yml").write_text(json.dumps([
        {"id": "agent-default-model", "config": {"provider": "loopback", "model": "fixture"}},
        {"id": "llm-pi-ai", "config": {"providers": {"loopback": {"api": "anthropic-messages",
         "baseURL": url, "apiKeyEnv": "FIXTURE_KEY", "models": [{"id": "fixture", "contextWindow": 65536,
         "maxTokens": 2048, "input": ["text", "image"]}]}}}}]))

    def launch(*argv):
        # Same duplex domain transport as the hosted runner. Reopen persisted domain state on resume.
        native = domain.NativeProject(root)
        left, right = socket.socketpair()
        def observe(op, args):
            result = native.invoke(op, args)
            if op in {"review_snapshot", "deliver"}:
                operations.append((op, result))
            if op == "deliver":
                with fitz.open(root / "paper/main.pdf") as pdf:
                    links = {link.get("nameddest") for link in pdf[0].get_links()}
                refs = domain.citation.parse_bib(str(root / "paper/references.bib"))
                rendered.append(("cite.method" in links, len(refs)))
            return result

        def serve():
            with left, left.makefile("rw") as conn:
                domain.serve(SimpleNamespace(invoke=observe), conn, conn)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        env = dict(PATH=os.environ["PATH"], HOME=str(home), DSH_HOME=str(home), DSH_TELEMETRY_DISABLED="1",
                   DEEPSEEK_API_KEY="fixture-only", DEEPSEEK_BASE_URL=url, FIXTURE_KEY="localhost",
                   ARK_DSH_API_KEY="fixture-only", IDEA2PAPER_DOMAIN_FD=str(right.fileno()), IDEA2PAPER_HOSTED="1")
        if "PKG_NATIVE_CACHE_PATH" in os.environ:
            env["PKG_NATIVE_CACHE_PATH"] = os.environ["PKG_NATIVE_CACHE_PATH"]
        with right:
            result = subprocess.run([dsh, "--profile", "idea2paper", *argv],
                                    env=env, pass_fds=(right.fileno(),), capture_output=True, text=True, timeout=150)
        thread.join(5)
        assert not errors, (errors, [r.get("blockers") for _, r in operations])
        assert result.returncode == 0, result.stderr[-1500:]
    try:
        with patch.object(domain.citation, "verify_bib", offline_bib), patch.object(
                domain.literature_evidence, "resolve", offline_source):
            launch("run", str(root), "private-owner-canary: deliver the test manuscript")
            first = json.loads((state / "native_review.json").read_text())
            launch("resume", str(root), "Revise and submit the new edition.")
    finally:
        server.shutdown()
        server.server_close()
    assert not owner_steps
    def calls(name):
        return [value for op, value in operations if op == name]
    snapshots = calls("review_snapshot")
    refused = [x for x in snapshots if x.get("blockers")]
    assert len(refused) == 2
    assert rendered == [(True, 1), (False, 1), (False, 2), (True, 2), (True, 2)]
    for rejection in refused:
        reasons = " ".join(rejection["blockers"])
        assert "column_overflow" in reasons and "bibliography" in reasons
    deliveries = calls("deliver")
    assert [x["delivered"] for x in deliveries] == [True, False, False, False, True]
    for x in deliveries[1:4]:
        assert "Independent review is missing, stale, or requires revision." in x["blockers"]
    assert deliveries[-2]["blockers"] == ["Independent review is missing, stale, or requires revision."]
    current = json.loads((state / "native_review.json").read_text())
    owner = json.loads((root / "workflow.json").read_text())["owner_session_id"]
    assert len({first["session_id"], current["session_id"], owner}) == 3
    assert first["revision"] != current["revision"] == deliveries[-1]["revision"]
    assert current["artifact_hashes"]["paper/main.pdf"] == digest(root / "paper/main.pdf")
    assert current["checks"]["citations"]["evidence"][0]["artifact"] == SOURCE
    assert current["previous_revision"] == first["revision"]
    for signed in (first, current):
        archived = state / "reviews" / signed["revision"]
        assert json.loads((archived / "review.json").read_text()) == signed
        assert digest(archived / "main.pdf") == signed["paper_pdf_sha256"]
        assert list((home / "sessions").glob("*/" + signed["session_id"]))
    return {"revision": current["revision"], "pdf_sha256": current["paper_pdf_sha256"],
            "deliveries": [x["delivered"] for x in deliveries]}


def test_signed_editorial_revision_on_dsh(tmp_path):
    import pytest

    profile, cli = os.getenv("IDEA2PAPER_TEST_PROFILE"), os.getenv("IDEA2PAPER_TEST_DSH")
    if not profile or not cli:
        pytest.skip("Requires an installed DSH profile and CLI")
    verify = runpy.run_path(str(ROOT / "integrations/dsh/verify.py"))
    verify["verify_editorial"](Path(profile), cli, tmp_path)


if __name__ == "__main__":
    installed, cli, scratch, runtime = sys.argv[1:]
    if Path(domain.__file__).resolve() != Path(runtime) / "ark/orchestrator/native_domain.py":
        raise RuntimeError("The editorial replay did not import the installed profile's Python companion.")
    if not __debug__:
        raise RuntimeError("Run the editorial acceptance test with assertions enabled.")
    print(json.dumps(editorial_revision(Path(installed), cli, Path(scratch))))
