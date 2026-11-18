import os
import yaml

from ark.delivery_contract import DeliveryReport, Finding, write_report
from website.dashboard.routes import _read_delivery_report


def test_delivery_summary_tracks_the_checked_artifacts(tmp_path):
    paper = tmp_path / "paper"
    paper.mkdir()
    pdf = paper / "main.pdf"
    pdf.touch()
    assert _read_delivery_report(tmp_path) is None
    page_detail = "3 body pages / limit 2 (4 total pages)"
    checks = [Finding("pdf_exists", "pass", "hard"),
              Finding("page_budget", "violated", "soft", page_detail),
              Finding("references_nonempty", "skipped", "hard"),
              Finding("figures_referenced", "violated", "soft", "Unreferenced figure"),
              Finding("disclosure_present", "violated", "hard", "Disclosure missing")]
    report = write_report(tmp_path, DeliveryReport(checks))
    assert _read_delivery_report(tmp_path) == {
        "status": "blocked", "hard": 1, "soft": 2, "skipped": 1,
        "pages": page_detail, "legacy_pages": False, "issues": ["Disclosure missing", "Unreferenced figure"],
    }
    previous = yaml.safe_load(report.read_text())
    previous.pop("schema_version")
    report.write_text(yaml.safe_dump(previous))
    assert _read_delivery_report(tmp_path)["legacy_pages"]
    checked = report.stat().st_mtime_ns
    tex = paper / "main.tex"
    tex.touch()
    os.utime(tex, ns=(checked + 1_000_000_000,) * 2)
    assert _read_delivery_report(tmp_path) == {"status": "stale"}
    tex.unlink()
    pdf.unlink()
    assert _read_delivery_report(tmp_path) == {"status": "stale"}
    report.write_text("findings: []")
    assert _read_delivery_report(tmp_path) == {"status": "unavailable"}
