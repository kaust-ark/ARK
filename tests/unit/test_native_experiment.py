import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ark.compute import native_experiment as compute


def project(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/study.py").write_text('print("[1, 2]")')
    return {"script": "scripts/study.py", "expected": "results/one.json", "question": "How do paired trials compare?"}


def test_ambiguous_submission_never_resubmits_and_stop_reconciles(monkeypatch, tmp_path):
    plan = project(tmp_path)
    config = {"experiment_compute_backend": {"type": "slurm", "account": ""}}
    submitted = []
    monkeypatch.setattr(compute, "_slurm_command", lambda *_: ["sbatch"])
    def invoke(args, **kw):
        submitted.append(args[0])
        if args[0] == "sbatch":
            raise subprocess.TimeoutExpired("sbatch", 1)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(compute, "_run", invoke)
    monkeypatch.setattr(compute, "_slurm_ids", lambda *a: None)
    with pytest.raises(subprocess.TimeoutExpired):
        compute.execute(tmp_path, config, plan, plan["script"])
    assert compute.execute(tmp_path, config, plan, plan["script"])["status"] == "submission_unknown"
    compute.cancel(tmp_path)
    assert compute.execute(tmp_path, config, plan, plan["script"])["status"] == "cancelling"
    monkeypatch.setattr(compute, "_slurm_ids", lambda *a: "7")
    monkeypatch.setattr(compute, "_job_state", lambda *a: ("CANCELLED", "0:0"))
    result = compute.execute(tmp_path, config, plan, plan["script"])
    assert result["status"] == "cancelled" and result["job_id"] == "7" and submitted == ["sbatch"]
    assert not (tmp_path / "results/one.json").exists()


def test_reuse_versioned_measurement_and_reject_overwrite(monkeypatch, tmp_path):
    plan = project(tmp_path)
    jobs = []
    def submit(stage, conf, rec, path, plot):
        jobs.append(rec["identity"])
        rec["job_id"] = str(len(jobs))
        (stage / "stdout").write_text("[1,2]")
        return "computed"
    monkeypatch.setattr(compute, "_slurm", submit)
    cfg = {"experiment_compute_backend": {"type": "slurm"}}
    a = compute.execute(tmp_path, cfg, plan, plan["script"])
    assert compute.execute(tmp_path, cfg, plan, plan["script"]) == a
    changed = {**plan, "question": "What changed on a second workload?"}
    with pytest.raises(ValueError, match="distinct filename"):
        compute.execute(tmp_path, cfg, changed, plan["script"])
    second = compute.execute(tmp_path, cfg, {**changed, "expected": "results/two.json"}, plan["script"])
    assert second["identity"] != a["identity"] and len(jobs) == 2
    (tmp_path / a["artifact"]).write_text("changed")
    with pytest.raises(ValueError, match="artifact changed"):
        compute.execute(tmp_path, cfg, plan, plan["script"])


def test_resources_and_credential_isolation(monkeypatch, tmp_path):
    image = tmp_path / "science.sif"
    image.touch()
    monkeypatch.setattr(compute, "apptainer_bin", lambda: "/usr/bin/apptainer")
    monkeypatch.setattr(compute, "sandbox_sif_path", lambda: str(image))
    args = compute._slurm_command(tmp_path, {"account": "", "partition": "mc"}, "i2p-fixture")
    assert "--account" not in args and "--export=NONE" in args
    assert "--network none" in args[-1] and "env -i" in args[-1] and "ulimit -f" in args[-1]
    assert ":/study:ro" in args[-1] and ":/output:rw" not in args[-1]


def test_stdout_overflow_persists_terminal_failure(monkeypatch, tmp_path):
    plan = project(tmp_path)
    cfg = {"experiment_compute_backend": {"type": "slurm"}}
    def oversized(staging, *args):
        with (staging / "stdout").open("wb") as f:
            f.truncate(compute.MAX_BYTES + 1)
        return "computed"
    monkeypatch.setattr(compute, "_slurm", oversized)
    with pytest.raises(ValueError, match="bounded regular"):
        compute.execute(tmp_path, cfg, plan, plan["script"])
    with pytest.raises(RuntimeError, match="Invalid experiment artifact"):
        compute.execute(tmp_path, cfg, plan, plan["script"])
    assert not (tmp_path / plan["expected"]).exists()


def test_input_or_staging_symlink_rejected(tmp_path):
    plan = project(tmp_path)
    file = tmp_path / plan["script"]
    file.unlink()
    file.symlink_to(tmp_path / "secret.py")
    with pytest.raises(ValueError, match="unlinked"):
        compute.execute(tmp_path, {}, plan, plan["script"])


def test_measurement_receipt_lookup_excludes_plots_and_checks_plan(tmp_path, monkeypatch):
    plan = project(tmp_path)
    def fake(staging, conf, row, path, plot):
        row["job_id"] = "123"
        return "running"
    monkeypatch.setattr(compute, "_slurm", fake)
    cfg = {"experiment_compute_backend": {"type": "slurm"}}
    record = compute.execute(tmp_path, cfg, plan, plan["script"])
    assert compute.measurement_receipt(tmp_path, plan, record["code_sha256"]) == record
    assert compute.measurement_receipt(tmp_path, {**plan, "question": "Another question"}, record["code_sha256"]) is None
    assert compute.measurement_receipt(tmp_path, plan, "wrong hash") is None


def test_local_crash_cannot_be_resubmitted_on_another_daemon(monkeypatch, tmp_path):
    row = {"identity": "i2p-fixture", "backend": "local", "status": "prepared"}
    path = tmp_path / "receipt.json"
    def daemon(name):
        monkeypatch.setattr(compute, "_run", lambda *a, **k: subprocess.CompletedProcess(a, 0, name, ""))
    daemon("original-daemon")
    compute._check_daemon(row, path)
    frozen = json.loads(path.read_text())
    assert frozen["docker_daemon"] == "original-daemon"
    daemon("other-daemon")
    with pytest.raises(RuntimeError, match="Reconnect"):
        compute._check_daemon(frozen, path)
    assert json.loads(path.read_text()) == row


@pytest.mark.skipif(sys.platform != "linux", reason="the isolated science worker runs Linux")
@pytest.mark.parametrize("read_input", [True, False])
def test_worker_observes_actual_measurement_reads(tmp_path, read_input):
    (tmp_path / "results.json").write_text('{"color": 73}')
    color = "json.loads(Path('results.json').read_text())['color']" if read_input else "73"
    (tmp_path / "main.py").write_text(
        "import json\nfrom pathlib import Path\nfrom PIL import Image\n"
        f"Image.new('L', (1, 1), {color}).save('figure.png')\n"
    )
    runner = compute.RUNNER.replace("/study", str(tmp_path)).replace("/output", str(tmp_path))
    completed = subprocess.run([sys.executable, "-I", "-c", runner, "figure.png"], capture_output=True)
    assert (tmp_path / "figure.png").is_file()
    if read_input:
        from PIL import Image
        assert completed.returncode == 0 and completed.stdout.startswith(compute.PLOT_READ_HEADER)
        with Image.open(io.BytesIO(base64.b64decode(completed.stdout.split(b"\n", 1)[1]))) as image:
            assert image.getpixel((0, 0)) == 73
    else:
        assert completed.returncode != 0 and b"did not read" in completed.stderr
        assert completed.stdout == b""


def test_old_plot_receipt_resumes_once_without_inventing_read_evidence(tmp_path, monkeypatch):
    plan = project(tmp_path)
    cfg = {"experiment_compute_backend": {"type": "slurm"}}
    (tmp_path / "results").mkdir()
    (tmp_path / plan["expected"]).write_text("[1,2]")
    filename = "past.png"
    calls = []
    def start(staging, conf, row, path, plot):
        calls.append(row["identity"])
        row["job_id"] = "42"
        return "running"
    monkeypatch.setattr(compute, "_slurm", start)
    row = compute.execute(tmp_path, cfg, plan, plan["script"], plot=filename)
    staging = tmp_path / "auto_research/state/native_runs" / row["identity"].removeprefix("i2p-")
    row.pop("plot_protocol")
    (staging / "receipt.json").write_text(json.dumps(row))

    from PIL import Image
    encoded = io.BytesIO()
    Image.new("L", (1, 1)).save(encoded, "PNG")
    (staging / "stdout").write_bytes(base64.b64encode(encoded.getvalue()))
    with pytest.raises(ValueError, match="did not confirm"):
        compute._publish(tmp_path, staging, tmp_path / "paper/figures" / filename, filename, {**row, "plot_protocol": 1})
    def reconcile(staging, conf, old, path, plot):
        calls.append(old["identity"])
        assert old["job_id"] == "42"
        return "computed"
    monkeypatch.setattr(compute, "_slurm", reconcile)
    saved = compute.execute(tmp_path, cfg, plan, plan["script"], plot=filename)
    assert saved["status"] == "completed" and "input_access" not in saved
    assert compute.execute(tmp_path, cfg, plan, plan["script"], plot=filename) == saved
    assert calls == [row["identity"], row["identity"]]


@pytest.mark.parametrize("kind", ["line", "points", "bar"])
def test_declarative_plot_draws_unrounded_nested_columns(tmp_path, monkeypatch, kind):
    from matplotlib.axes import Axes
    import fitz
    plan = project(tmp_path)
    (tmp_path / "results").mkdir()
    xs = [1, 2] if kind == "line" else ["Base", "Extended"]
    data = {"batch/1": {"~pairs": [
        {"n": x, "scores": {"baseline": a, "method": b}}
        for x, a, b in zip(xs, [0.023417, 0.88706], [0.020395, 0.74456])
    ]}}
    measured = tmp_path / plan["expected"]
    measured.write_text(json.dumps(data))
    spec = {"kind": kind, "rows_pointer": "/batch~11/~0pairs", "x": {"path": "n", "label": "Condition"},
            "series": [{"path": "scores.baseline", "label": "Comparator"}, {"path": "scores.method", "label": "Method"}],
            "y_label": "Loss (units)"}
    observed = {}
    primitive = {"line": "plot", "points": "scatter", "bar": "bar"}[kind]
    draw = getattr(Axes, primitive)
    def observe(ax, x, y, *args, **kwargs):
        if "label" in kwargs:
            observed[kwargs["label"]] = list(y)
        return draw(ax, x, y, *args, **kwargs)
    monkeypatch.setattr(Axes, primitive, observe)
    def render(staging, conf, row, path, filename):
        assert b"0.023417" not in (staging / "input/main.py").read_bytes()
        code = (staging / "input/main.py").read_text().replace("/study", str(staging / "input")).replace("/output", str(staging))
        exec(compile(code, str(staging / "input/main.py"), "exec"), {})
        (staging / "stdout").write_bytes(compute.PLOT_READ_HEADER + base64.b64encode((staging / filename).read_bytes()))
        row["job_id"] = "42"
        return "computed"
    monkeypatch.setattr(compute, "_slurm", render)
    cfg = {"experiment_compute_backend": {"type": "slurm"}}
    with pytest.raises(ValueError, match="worker receipt"):
        compute.execute(tmp_path, cfg, plan, None, plot="comparison.pdf", selection=spec, measurement_sha256="wrong")
    row = compute.execute(tmp_path, cfg, plan, None, plot="comparison.pdf", selection=spec,
                          measurement_sha256=hashlib.sha256(measured.read_bytes()).hexdigest())
    assert row["input_access"] == "worker_observed_read" and row["plot_spec"]["kind"] == kind
    assert observed == {"Comparator": [0.023417, 0.88706], "Method": [0.020395, 0.74456]}
    with fitz.open(tmp_path / row["artifact"]) as doc:
        for label in ("Comparator", "Method", "Condition", "Loss (units)"):
            assert label in doc[0].get_text()
