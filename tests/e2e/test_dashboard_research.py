"""Exercise the research desk through its mounted HTTP routes and real EventSource."""
import base64
import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

import pytest


def test_live_research_desk(tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    import fitz
    import uvicorn
    from fastapi import FastAPI
    from itsdangerous import TimestampSigner
    from starlette.middleware.sessions import SessionMiddleware
    from starlette.staticfiles import StaticFiles
    from website.dashboard import config, db, routes
    from website.dashboard.auth import make_share_token

    monkeypatch.setattr(config, "_load_env_file", lambda: {})
    settings = config.Settings()
    settings.db_path = str(tmp_path / "isolated.db")
    settings.projects_root = tmp_path
    settings.secret_key = "local-browser-fixture"
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "_engine", None)
    with db.get_session(settings.db_path) as session:
        owner, _ = db.get_or_create_user_by_email(session, "researcher@example.test")
        uid = owner.id
        p = db.create_project(session, user_id=uid, name="Sorting study", title="Search cost on sorted inputs",
                              activity="Checking the crossover in search cost", phase="dev", status="running")
        pid = p.id
        decision = db.create_pending_decision(session, pid, "Which input sizes should be measured next?",
                                              ["Extend to 1,024", "Repeat at 128"], context="The current run ends at 128 items.")
    root = tmp_path / uid / pid
    state = root / "auto_research/state"
    state.mkdir(parents=True)
    (root / "results").mkdir()
    (root / "paper").mkdir()
    from PIL import Image
    figure = root / "paper/figures/comparisons.png"
    figure.parent.mkdir()
    Image.new("RGB", (80, 50), "green").save(figure)
    figure_digest = hashlib.sha256(figure.read_bytes()).hexdigest()
    measurement = root / "results/experiment.json"
    measurement.write_text(json.dumps({"rows": [{"label": "128 items", "baseline": 128, "proposed": 7}]}))
    journal = state / "research_events.jsonl"

    def event(ident, phase, message, **attrs):
        with journal.open("a") as f:
            f.write(json.dumps({"id": ident, "ts": datetime.now(timezone.utc).isoformat(), "action": "measurement",
                                "phase": phase, "observation": message, **attrs}) + "\n")

    event("old-comparison", "review", "Recomputed 128 items.", action="measurement_reproduction_finished",
          condition="128 items", differed=1, outcomes=2, metric="search_comparisons",
          reported_value="7", independent_value="0")
    event("observed", "experiments", "Recorded search comparisons for 128 items.", artifact="results/experiment.json")
    previous = None
    for label, claim in [("Reversed", "Arrow points to the wrong series"), ("Corrected", "Arrow points to the baseline")]:
        with fitz.open() as doc:
            pg = doc.new_page()
            pg.insert_text((60, 80), label + " plot")
            data = doc.tobytes()
        digest = hashlib.sha256(data).hexdigest()
        revision = hashlib.sha256(label.encode()).hexdigest()
        archived = state / "reviews" / revision
        archived.mkdir(parents=True)
        (archived / "main.pdf").write_bytes(data)
        source = "Discussion. " + claim + "."
        (archived / "main.tex").write_text(source)
        (root / "paper/main.tex").write_text("Author's newer draft.")
        (root / "paper/main.pdf").write_bytes(data)
        method = "auto_research/state/native_runs/" + "a" * 24 + "/input/main.py"
        method_sha = hashlib.sha256(b"return total / trials").hexdigest()
        control = {"artifact": method, "sha256": method_sha, "target": "mean(comparisons)",
                   "expected": 2.5, "observed": 5, "matches": False,
                   "rationale": "Two trials should halve the total. Check <img src=x> literally."}
        record = {"revision": revision, "previous_revision": previous, "verdict": "unassessed" if previous else "revise",
                  "summary": "Compared the annotation with the plotted series.", "paper_pdf_sha256": digest,
                  "artifact_hashes":{"paper/figures/comparisons.png":figure_digest, method:method_sha},
                  "checks":{"experiment":{"status":"unassessed","reason":"The averaging control disagreed with its prediction.",
                             "controls":[control, {**control, "target":"probe_counts()", "matches":True,
                                                   "expected":[9007199254740993, 0], "observed":[9007199254740993, 0]},
                                         {**control, "control":2, "target":"Recorded condition", "columns":["temperature","accuracy"],
                                          "expected":{"temperature":64,"accuracy":0.9}, "observed":{"temperature":16,"accuracy":0.9},
                                          "differences":["temperature"]},
                                         {**control, "sha256":"0" * 64, "target":"unverified_method()"}],
                             "evidence":[{"artifact":"scripts/measure.py","quote":"return total / trials",
                                          "sha256":hashlib.sha256(b"return total / trials").hexdigest()},
                                         {"artifact":"paper/main.tex","quote":claim,
                                          "sha256":hashlib.sha256(source.encode()).hexdigest()}]},
                            "visual":{"status":"assessed","reason":"Compared the two archived pages.",
                                      "series_origin":{"comparisons.png":"measured"}}},
                  "issues": [{"id":"R1","severity":"blocker","claim":claim,"reason":"Compared at the same axis scale.",
                              "status":"resolved" if previous else "new","evidence":[{"artifact":"paper/main.pdf",
                              "page":1,"visual":True,"sha256":digest},{"artifact":"paper/main.tex",
                              "quote":claim,"sha256":hashlib.sha256(source.encode()).hexdigest()}]}]}
        (archived / "review.json").write_text(json.dumps(record))
        if previous is None:
            archived_pdf = f"auto_research/state/reviews/{revision}/main.pdf"
            archived_digest = digest
            event("archived-review", "review", "revise", action="manuscript_reviewed", artifact=archived_pdf)
        previous = revision
    (state / "native_review.json").write_text(json.dumps(record))

    app = FastAPI()
    sub = FastAPI()
    sub.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
    sub.include_router(routes.router)
    sub.mount("/static", StaticFiles(directory=routes._STATIC_DIR))
    app.mount("/dashboard", sub)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off", timeout_graceful_shutdown=2))
    server_thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    server_thread.start()
    try:
        with pw.sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                pytest.skip("Chromium not installed")
            browser = playwright.chromium.launch(executable_path=playwright.chromium.executable_path)
            for width in (1300, 390):
                ctx = browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
                cookie = TimestampSigner(settings.secret_key).sign(base64.b64encode(json.dumps({"user_id":uid}).encode()))
                ctx.add_cookies([{"name":"session","value":cookie.decode(),"url":base}])
                page = ctx.new_page()
                # Observe actual Web Animations without depending on a 220 ms screenshot window.
                page.add_init_script("""const animate = Element.prototype.animate;
                    window.observationMotion = [];
                    Element.prototype.animate = function(...args) {
                        if (this.id === 'research-observation') window.observationMotion.push(this.textContent);
                        return animate.apply(this, args);
                    };""")
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.route("https://**", lambda r: r.abort())
                # Persist a milestone after the HTTP snapshot but before the very first SSE read.
                if width == 1300:
                    def gap(route):
                        event("gap", "experiments", "Second batch returned.", count=2, total=3)
                        event("gap", "experiments", "Second batch returned.", count=2, total=3)
                        # The first connection ends without a data event. The
                        # browser reconnects using the HTTP snapshot's cursor.
                        route.fulfill(content_type="text/event-stream", body="retry: 100\n\n")
                    page.route("**/stream?*", gap, times=1)
                url = base + f"/dashboard/#project/{pid}"
                page.goto(url, wait_until="domcontentloaded")
                page.get_by_text("Second batch returned. (2/3)", exact=True).first.wait_for()
                meter = page.locator("#research-count span")
                assert meter.is_visible()
                assert abs(meter.evaluate("el => el.getBoundingClientRect().width / el.parentNode.clientWidth") - 2/3) < .01
                assert meter.evaluate("el => getComputedStyle(el).transitionDuration") == "0s"
                assert page.evaluate("window.observationMotion") == []
                experiment = page.locator('#research-phases button[data-phase="experiment"]')
                experiment.click()
                assert experiment.get_attribute("aria-pressed") == "true"
                assert page.locator("#research-history").get_attribute("open") is not None
                history = page.locator("#research-history")
                result_file = history.get_by_role("button", name="Open current file · experiment.json")
                result_file.focus()
                output = os.environ.get("IDEA2PAPER_UI_CAPTURE")
                def capture_history():
                    desk = page.locator("#research-desk")
                    desk.evaluate("el => window.scrollBy(0, el.getBoundingClientRect().top - document.querySelector('#main-nav').offsetHeight - 8)")
                    desk.screenshot(path=str(Path(output) / f"evidence-history-{width}.png"))
                if output and width == 390:
                    capture_history()
                event(f"focus-{width}", "experiments", "Finished reading result rows.", action="tool/result")
                history.locator(f'li[data-event="focus-{width}"]').wait_for()
                assert result_file.evaluate("el => document.activeElement === el")
                page.keyboard.press("Enter")
                page.locator("#preview-content td").first.wait_for()
                assert page.locator("#preview-content td").all_text_contents() == ["128 items", "128", "7"]
                page.keyboard.press("Escape")
                assert result_file.evaluate("el => document.activeElement === el")
                page.locator("#history-all").click()
                assert experiment.get_attribute("aria-pressed") == "false"
                assert page.locator("#research-history li").filter(has_text="Second batch").count() == 1
                archive_file = history.get_by_role("button", name="Open archived file · main.pdf")
                assert archive_file.locator("..").get_by_text("Needs revision", exact=True).is_visible()
                if output and width == 1300:
                    capture_history()
                archive_file.click()
                archive_url = page.locator("#evidence-open").get_attribute("href")
                assert hashlib.sha256(page.request.get(base + archive_url).body()).hexdigest() == archived_digest
                page.locator("#evidence-close").click()
                assert archive_file.evaluate("el => document.activeElement === el")
                earlier = history.locator('li[data-event="old-comparison"] details')
                earlier.locator("summary").focus()
                page.keyboard.press("Enter")
                assert earlier.locator("bdi").inner_text() == "search_comparisons"
                assert earlier.locator("dd").all_inner_texts() == ["7", "0"]
                assert earlier.locator("dl").evaluate("el => getComputedStyle(el).animationName") == "none"
                if width == 390:
                    event("new-match", "review", "Recomputed 256 items.",
                          action="measurement_reproduction_finished", condition="256 items", differed=0, outcomes=2)
                    page.locator("#research-observation").get_by_text("256 items · All 2 outcomes matched.").wait_for()
                    assert page.locator("#research-comparison").is_hidden()
                    assert earlier.locator("dd").first.is_visible()
                    assert earlier.locator("summary").evaluate("el => el === document.activeElement")
                    assert history.locator('li[data-event="new-match"] details').count() == 0
                history.locator(":scope > summary").click()
                if width == 1300:
                    draft = "Add 256 items before doubling again."
                    page.locator("#decision-text").fill(draft)
                    page.reload(wait_until="domcontentloaded")
                    page.locator("#decision-text").wait_for()
                    assert page.locator("#decision-text").input_value() == draft
                    page.locator("#decision-form").get_by_role("button", name="Send instruction").click()
                    page.locator("#hitl-decision").wait_for(state="hidden")
                page.locator("#evidence-files button").filter(has_text="experiment.json").click()
                page.locator("#preview-content td").first.wait_for()
                assert page.locator("#preview-content td").all_text_contents() == ["128 items", "128", "7"]
                page.locator("#evidence-close").focus()
                page.keyboard.press("Enter")
                assert page.evaluate("document.activeElement.dataset.key") == "results/experiment.json"
                before = page.locator("#review-issues button").filter(has_text="Previous revision · paper/main.pdf")
                after = page.locator("#review-issues button").filter(has_text="Reviewed revision · paper/main.pdf")
                page.locator("#review-coverage > summary").click()
                controls = page.locator(".method-controls")
                controls.locator("summary").focus()
                page.keyboard.press("Enter")
                assert controls.locator("summary").inner_text() == "Experimental controls (3)"
                mismatch, match, reproduced = controls.get_by_role("listitem").all()
                values = reproduced.get_by_role("table", name="Recorded condition")
                assert values.get_by_role("row").nth(1).locator("td").all_text_contents() == ["64","16","Mismatch"]
                assert values.get_by_role("row").nth(2).locator("td").all_text_contents() == ["0.9","0.9","Matches"]
                assert mismatch.locator("strong").inner_text() == "mean(comparisons) · Mismatch"
                assert mismatch.locator("dt").all_text_contents() == ["Expected", "Observed", "Reviewer’s rationale", "Source"]
                assert mismatch.locator("dd").all_text_contents() == [
                    "2.5", "5", control["rationale"], method + " · SHA-256 " + method_sha,
                ]
                assert match.locator("strong").inner_text() == "probe_counts() · Matches"
                assert match.locator("dd").all_text_contents()[:2] == ["[9007199254740993, 0]"] * 2
                assert controls.locator("img,a,button").count() == 0
                passages = page.locator(".review-passages")
                passages.locator("summary").click()
                assert passages.locator("strong").inner_text() == "scripts/measure.py"
                assert passages.locator("pre").first.inner_text() == "return total / trials"
                passages.get_by_role("button", name="paper/main.tex").click()
                assert page.locator("#preview-content mark").inner_text() == claim
                page.locator("#evidence-close").click()
                assert "Compared the two archived pages." in page.locator("#review-coverage").inner_text()
                assert "Plotted from measurements" in page.locator(".review-figures").inner_text()
                page.get_by_role("button", name="comparisons.png", exact=True).click()
                page.wait_for_function("document.querySelector('#preview-content img')?.naturalWidth === 80")
                assert figure_digest in page.locator("#preview-content img").get_attribute("src")
                page.locator("#evidence-close").click()
                assert "Blocking issue" in page.locator("#review-issues li strong").inner_text()
                page.locator("#review-issues button").filter(has_text="Previous revision · paper/main.tex").click()
                assert page.locator("#preview-content mark").inner_text() == "Arrow points to the wrong series"
                assert "Author's newer draft" not in page.locator("#preview-content").inner_text()
                page.locator("#evidence-close").click()
                before.click()
                img = page.locator("#preview-content img")
                page.wait_for_function("document.querySelector('#preview-content img')?.naturalWidth > 0")
                first_src = img.get_attribute("src")
                after.click()
                page.wait_for_function("document.querySelector('#preview-content img')?.naturalWidth > 0")
                assert first_src != img.get_attribute("src") and "&page=1" in img.get_attribute("src")
                bad_digest = img.get_attribute("src").replace(record["paper_pdf_sha256"], "0" * 64)
                assert page.request.get(base + bad_digest).status == 409
                assert page.locator("#research-diagnostics").get_attribute("open") is None
                overflow = page.evaluate("""[...document.querySelectorAll('body *')].filter(el =>
                    el.getBoundingClientRect().right > innerWidth + 1).map(el => el.id || el.className || el.tagName).slice(0,16)""")
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), overflow
                assert not errors
                # Local inspection without generating repo artifacts.
                if output := os.environ.get("IDEA2PAPER_UI_CAPTURE"):
                    page.locator("#evidence-close").click()
                    page.evaluate("window.scrollTo(0,0)")
                    page.screenshot(path=str(Path(output) / f"research-{width}.png"))
                if width == 390:
                    diagnostic = "./main.tex:56: Undefined control sequence."
                    event("typeset", "writing", diagnostic, action="paper_compile_failed")
                    page.locator("#research-observation").get_by_text(
                        "Typesetting error in main.tex, line 56", exact=True
                    ).wait_for()
                    details = page.locator("#research-build-details")
                    assert details.get_attribute("open") is None
                    details.locator("summary").focus()
                    page.keyboard.press("Enter")
                    assert details.locator("pre").inner_text() == diagnostic
                    page.evaluate("setLang('zh')")
                    page.get_by_text("实验控制项（3）", exact=True).wait_for(state="attached")
                    history.get_by_text("打开归档文件 · main.pdf", exact=True).wait_for(state="attached")
                    assert "不一致" in controls.text_content() and "评审理由" in controls.text_content()
                    page.emulate_media(reduced_motion="no-preference")
                    # Switching languages reconnects the stream. The initial snapshot
                    # restores the screen silently; only a subsequent live event animates.
                    page.wait_for_function("streamReady && !disconnected && detailEventSource?.readyState === EventSource.OPEN")
                    event("abi", "review", "Invalid row.", action="measurement_reproduction_failed",
                          failure="row_format", attempt=2)
                    row_error = "第 2 次复算返回的数据格式有误，尚无可比较的数值。"
                    page.locator("#research-observation").get_by_text(row_error, exact=True).wait_for()
                    assert page.evaluate("window.observationMotion") == [row_error]
                    event("abi", "review", "Invalid row.", action="measurement_reproduction_failed",
                          failure="row_format", attempt=2)
                    event("real-row", "review", "Recomputed seed 2.", action="measurement_reproduction_finished",
                          condition="seed 2", differed=3, outcomes=6, metric="best_temp",
                          reported_value="16", independent_value="64")
                    page.locator("#research-observation").get_by_text(
                        "seed 2 · 3 项存在差异，共 6 项。", exact=True
                    ).wait_for()
                    comparison = page.locator("#research-comparison")
                    assert comparison.locator("bdi").inner_text() == "best_temp"
                    assert comparison.locator("dt").all_text_contents() == ["论文报告值", "独立复算"]
                    assert comparison.locator("dd").all_text_contents() == ["16", "64"]
                    assert "独立复算为 64" in page.locator("#research-announce").inner_text()
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                    assert page.evaluate("window.observationMotion").count(row_error) == 1
                    page.evaluate("setLang('ar')")
                    page.get_by_text("ضوابط التجربة (3)", exact=True).wait_for(state="attached")
                    history.get_by_text("فتح الملف الحالي · experiment.json", exact=True).wait_for(state="attached")
                    assert "غير مطابق" in controls.text_content()
                    event("match", "review", "Recomputed seed 3.", action="measurement_reproduction_finished",
                          condition="seed 3", differed=0, outcomes=6)
                    comparison.wait_for(state="hidden")
                    assert "seed 3" in page.locator("#research-observation").inner_text()
                ctx.close()

            shared = browser.new_context().new_page()
            token = make_share_token(pid, settings.secret_key)
            shared.goto(base + f"/dashboard/share/{token}", wait_until="domcontentloaded")
            shared.locator("#research-observation").get_by_text("No observation recorded yet.").wait_for()
            assert shared.locator("#research-review").is_hidden()
            assert shared.locator("#research-history .history-file").count() == 0
            assert shared.request.get(base + f"/dashboard/api/projects/{pid}/evidence?key=results/experiment.json").status == 404
            assert shared.request.get(base + f"/dashboard/api/projects/{pid}/research-history?before=10").status == 404
            browser.close()
        with db.get_session(settings.db_path) as session:
            assert db.get_decision(session, decision).answer_text == draft
    finally:
        server.should_exit = True
        server_thread.join(10)
        sock.close()
