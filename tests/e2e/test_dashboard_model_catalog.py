"""Exercise the actual dashboard forms with a changing account catalog."""
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest


def test_owner_catalog_across_launch_settings_continue_and_restart():
    playwright = pytest.importorskip("playwright.sync_api")
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    folder = Path(__file__).resolve().parents[2] / "website/dashboard/templates"
    env = Environment(loader=FileSystemLoader(folder), autoescape=select_autoescape())
    html = env.get_template("app.html").render(app_base="/dashboard", share_mode=False,
                                               launcher_sa="", required_roles=[],
                                               provider_labels={"deepseek": "DeepSeek", "openai": "OpenAI"})
    direct = "deepseek/new-chat"
    routed = "openrouter/deepseek/new-chat"
    free = "openrouter/example/unseen:free"
    legacy = "claude-sonnet-retired"
    scope_ids = ("hero", "continue", "restart", "settings")
    catalog = {"default_launch_model": direct, "automatic_model": direct, "providers": [
        {"id": "openrouter", "label": "OpenRouter", "source": "provider", "models": [
            {"id": free, "name": "<img src=x>"},
            {"id": routed, "name": "New Chat"},
        ]},
        {"id": "deepseek", "label": "DeepSeek", "source": "cache", "models": [
            {"id": direct, "name": "New Chat"},
            *({"id": f"deepseek/family-{n}", "name": f"Family {n}"} for n in range(25)),
        ]},
    ]}
    owned = True
    calls = []
    fail_catalog = False

    def serve(route):
        request = route.request
        path = urlsplit(request.url).path.removeprefix("/dashboard")
        if path == "/":
            route.fulfill(body=html, content_type="text/html")
        elif path == "/api/me":
            route.fulfill(json={"authenticated": True, "has_keys": True})
        elif path == "/api/user/model-catalog":
            route.fulfill(status=503 if fail_catalog else 200, json=catalog)
        elif path == "/api/user/default-launch-model":
            model = request.post_data_json["model"]
            calls.append((path, model))
            catalog.update(default_launch_model=model or None, automatic_model=model or direct)
            route.fulfill(json={"default_launch_model": model or None})
        elif path == "/api/venues":
            route.fulfill(json=[{"name": "ICML", "format": "icml", "pages": 8}])
        elif path == "/api/projects/example":
            route.fulfill(json={"id": "example", "venue": "ICML", "model": legacy, "is_owner": owned})
        elif path == "/api/projects" and request.method == "POST":
            header = request.headers["content-type"]
            msg = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {header}\r\n\r\n".encode() + request.post_data_buffer)
            fields = {p.get_param("name", header="content-disposition"): p.get_content()
                      for p in msg.iter_parts()}
            calls.append((path, fields.get("model")))
            route.fulfill(status=409, json={"detail": "Recorded"})
        elif request.method == "POST" and path.endswith(("/continue", "/restart")):
            calls.append((path, request.post_data_json["model"]))
            route.fulfill(status=409, json={"detail": "Recorded"})
        else:
            route.fulfill(json=[] if path == "/api/projects" else {})

    with playwright.sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("install Chromium with playwright install chromium")
        browser = pw.chromium.launch(executable_path=pw.chromium.executable_path)
        page = browser.new_page()
        page.route("**/*", serve)
        page.goto("http://local.test/dashboard/")
        page.wait_for_function("document.querySelector('#hero-model-select optgroup') !== null")
        hero = page.locator("#hero-model-select")
        # Project pickers expose the catalog only after the server confirms ownership.
        for scope in ("continue", "restart"):
            assert page.locator(f"#{scope}-model-select optgroup").count() == 0
            page.evaluate(f"open{scope.capitalize()}Modal('example')")
            page.evaluate(f"closeModal('modal-{scope}')")
        for scope in scope_ids:
            picker = page.locator(f"#{scope}-model-select")
            assert picker.locator("optgroup").evaluate_all("xs => xs.map(x => x.label)") == ["OpenRouter", "DeepSeek"]
            assert picker.locator(f'option[value="{free}"]').text_content() == "<img src=x> (free)"
            assert picker.locator("img").count() == 0
        assert hero.input_value() == ""
        assert direct in hero.locator("option:checked").inner_text()
        launch = "performLaunch(document.querySelector('#hero-form'))"
        page.evaluate(launch)
        assert calls[-1] == ("/api/projects", None)
        hero.select_option(free, force=True)
        assert "limits free requests per day" in page.locator("#hero-model-note").text_content()
        page.evaluate(launch)
        assert calls[-1] == ("/api/projects", free)

        page.locator("#hero-model-search").evaluate(
            "(e) => { e.value = 'family-24'; e.dispatchEvent(new Event('input')); }")
        assert hero.locator("optgroup option").count() == 2  # keeps the explicit free pick
        assert hero.input_value() == free
        page.evaluate("loadModelCatalog(true, true)")
        assert hero.input_value() == free
        page.evaluate("openSettingsModal()")
        settings = page.locator("#settings-model-select")
        assert page.locator("#other-provider-name option").all_text_contents() == ["Provider…", "DeepSeek"]
        settings.select_option(routed)
        page.get_by_role("button", name="Set as my default").last.click()
        page.wait_for_function("modelCatalog?.default_launch_model === 'openrouter/deepseek/new-chat'")
        assert calls[-1] == ("/api/user/default-launch-model", routed)
        assert hero.input_value() == free
        page.locator("#settings-clear-default-model").click()
        page.wait_for_function("modelCatalog?.default_launch_model === null")
        assert calls[-1] == ("/api/user/default-launch-model", "")
        page.evaluate("closeModal('modal-settings')")

        page.evaluate("openContinueModal('example')")
        assert legacy in page.locator("#continue-model-select option:checked").inner_text()
        page.evaluate("submitContinue()")
        assert calls[-1] == ("/api/projects/example/continue", legacy)
        page.evaluate("closeModal('modal-continue')")
        page.evaluate("openRestartModal('example')")
        restart = page.locator("#restart-model-select")
        restart.select_option("__manual__")
        for invalid in ("unfinished", "openai/..", "xai/a/../b", "cohere_chat/a//b", "mistral/a?b"):
            page.locator("#restart-model-manual").fill(invalid)
            page.evaluate("submitRestart()")
            assert calls[-1][0].endswith("/continue")
        page.locator("#restart-model-manual").fill("mistral/custom-deployment")
        fail_catalog = True
        page.evaluate("loadModelCatalog(true)")
        assert "Could not refresh" in page.locator("#restart-model-note").text_content()
        page.evaluate("submitRestart()")
        assert calls[-1] == ("/api/projects/example/restart", "mistral/custom-deployment")
        owned = False
        for scope in ("continue", "restart"):
            page.evaluate(f"open{scope.capitalize()}Modal('example')")
            picker = page.locator(f"#{scope}-model-select")
            assert picker.locator("optgroup").count() == 0
            assert len(picker.locator("option").all()) == 2  # keep prior model or manual
            assert page.locator(f"#{scope}-model-note").text_content() == ""
            page.evaluate(f"closeModal('modal-{scope}')")
        fail_catalog = False
        catalog.update(providers=[], automatic_model=None)
        page.evaluate("loadModelCatalog(true, true)")
        assert not page.locator("#hero-model-no-keys").evaluate("el => el.hidden")
        assert not hero.locator("optgroup").count()
        assert page.evaluate("readModelPicker('continue')") == legacy
        previous = len(calls)
        page.evaluate(launch)
        assert len(calls) == previous
        browser.close()
