"""Browser regression for approving a research plan when its chat history is empty."""

from pathlib import Path
from urllib.parse import urlsplit

import pytest


def test_decision_survives_reconnect_and_failed_submission():
    playwright = pytest.importorskip("playwright.sync_api")
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    templates = Path(__file__).resolve().parents[2] / "website/dashboard/templates"
    env = Environment(loader=FileSystemLoader(templates), autoescape=select_autoescape())
    html = env.get_template("app.html").render(app_base="/dashboard", share_mode=False,
                                               launcher_sa="", required_roles=[])
    project = {"id": "owned", "status": "running", "max_iterations": 1, "pending_decision": {
        "id": "plan1", "kind": "experiment_approval", "question": "Approve experiment?",
        "context": "Baseline: insertion sort <img src=x onerror=alert(1)>",
        "options": ["Approve", "Custom — type your own instruction"],
        "default_index": 0, "timeout_action": "proceed_default", "deadline_at": "2030-01-01T00:00:00+00:00",
    }}
    posts = []

    def serve(route):
        path = urlsplit(route.request.url).path
        if path == "/dashboard/":
            route.fulfill(body=html, content_type="text/html")
        elif path.endswith("/owned/decision"):
            posts.append(route.request.post_data_json)
            route.fulfill(status=503 if len(posts) == 1 else 200, json={})
        elif path.endswith("/owned"):
            route.fulfill(json=project)
        elif path.endswith("/owned/messages"):
            route.fulfill(json={"messages": []})
        else:
            route.fulfill(json={})

    with playwright.sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("install Chromium with playwright install chromium")
        browser = pw.chromium.launch(executable_path=pw.chromium.executable_path)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.route("**/*", serve)
        page.add_init_script("""window.EventSource = class extends EventTarget {
            constructor(url) { super(); window.testStream = this; }
            close() {}
        }""")
        page.goto("http://local.test/dashboard/")
        page.locator("#page-login.active").wait_for()
        page.evaluate("navigate('detail', { id: 'owned' })")
        card = page.locator("#hitl-decision")
        card.get_by_role("button", name="Approve", exact=True).wait_for()
        assert card.locator("#decision-context img").count() == 0
        assert "<img" in card.locator("#decision-context").inner_text()
        assert "2030" in card.locator("#decision-default").inner_text()
        assert card.get_by_role("button", name="Custom", exact=False).count() == 0
        assert not page.locator("#hitl-compose").is_visible()
        draft = "Include the heap sort baseline"
        card.get_by_role("textbox").fill(draft)
        status = {key: value for key, value in project.items() if key != "id"}
        page.evaluate("""s => testStream.dispatchEvent(new MessageEvent('status', {
            data: JSON.stringify(s)
        }))""", status)
        assert card.get_by_role("textbox").input_value() == draft
        send = card.get_by_role("button", name="Send instruction")
        send.click()
        page.get_by_text("Could not send.", exact=False).wait_for()
        assert card.get_by_role("textbox").input_value() == draft
        send.click()
        card.wait_for(state="hidden")
        assert posts == [{"decision_id": "plan1", "index": -1, "text": draft}] * 2
        assert page.locator("#hitl-compose").is_visible()
        project["pending_decision"]["id"] = "plan2"
        page.evaluate("loadDetail('owned')")
        card.get_by_role("button", name="Approve", exact=True).click()
        card.wait_for(state="hidden")
        assert posts[-1] == {"decision_id": "plan2", "index": 0, "text": ""}
        browser.close()
