"""The saved delivery result stays with its paper as the owner navigates."""
from pathlib import Path

import pytest


def test_render_delivery_checks():
    playwright = pytest.importorskip("playwright.sync_api")
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    templates = Path(__file__).resolve().parents[2] / "website/dashboard/templates"
    env = Environment(loader=FileSystemLoader(templates), autoescape=select_autoescape())
    html = env.get_template("app.html").render(app_base="/dashboard", share_mode=False,
                                               launcher_sa="", required_roles=[])
    with playwright.sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("Chromium not installed")
        browser = pw.chromium.launch(executable_path=pw.chromium.executable_path)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.route("**/*", lambda route: route.fulfill(body=html, content_type="text/html")
                   if route.request.resource_type == "document" else route.fulfill(json={}))
        page.goto("http://local.test/dashboard/")
        page.evaluate("document.querySelector('#page-detail').classList.add('active')")
        project = {"id": "local", "has_pdf": True, "status": "done"}
        panel = page.locator("#delivery-report")
        render = "p => renderDetailActions(p)"
        page.evaluate(render, project)
        assert "No delivery report" in panel.inner_text()
        project["delivery_report"] = {
            "status": "blocked", "hard": 1, "soft": 1, "skipped": 1,
            "pages": "3 body pages / limit 2 (4 total pages)",
            "issues": ["Missing disclosure <img src=broken>"],
        }
        page.evaluate(render, project)
        assert "Failed required checks (1)" in panel.inner_text()
        assert "3 body pages / limit 2" in panel.inner_text()
        assert "Checks skipped (1)" in panel.inner_text()
        assert "<img src=broken>" in panel.inner_text() and panel.locator("img").count() == 0
        assert panel.bounding_box()["width"] <= 390
        page.evaluate(render, {**project, "delivery_report": {"status": "stale"}})
        assert "changed" in panel.inner_text() and "disclosure" not in panel.inner_text()
        page.evaluate("window.SHARE_MODE = true")
        page.evaluate(render, project)
        assert panel.is_hidden()
        browser.close()
