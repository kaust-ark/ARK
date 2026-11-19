import json
from pathlib import Path
import subprocess

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
