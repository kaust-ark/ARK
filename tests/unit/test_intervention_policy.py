"""Unit tests for ark.intervention.policy — risk classification."""

import pytest
from pathlib import Path

from ark.intervention.policy import (
    Category, Severity, InterventionPolicy, load_policy,
    ALLOW, NOTIFY, ASK, DENY,
)


WORK = [Path("/home/u/proj"), Path("/home/u/proj/results")]


def std():
    return InterventionPolicy(autonomy="standard")


# ── destructive_fs ─────────────────────────────────────────

def test_single_file_delete_in_workarea_allowed():
    d = std().classify_command("rm /home/u/proj/results/tmp.txt", work_dirs=WORK)
    assert d.action == ALLOW
    assert d.category == Category.DESTRUCTIVE_FS


def test_rm_rf_asks_under_standard():
    d = std().classify_command("rm -rf /home/u/proj/results/old", work_dirs=WORK)
    assert d.action == ASK
    assert d.severity == Severity.HIGH


def test_rm_outside_workarea_asks():
    d = std().classify_command("rm /etc/hosts", work_dirs=WORK)
    assert d.action == ASK
    assert d.category == Category.DESTRUCTIVE_FS


def test_delete_dotgit_is_critical():
    d = std().classify_command("rm -rf .git", work_dirs=WORK)
    assert d.action == ASK
    assert d.severity == Severity.CRITICAL


def test_git_reset_hard_asks():
    d = std().classify_command("git reset --hard HEAD~3", work_dirs=WORK)
    assert d.action == ASK
    assert d.category == Category.DESTRUCTIVE_FS


# ── bulk_compute ───────────────────────────────────────────

def test_single_sbatch_allowed():
    d = std().classify_command("sbatch train.sh", work_dirs=WORK)
    assert d.action == ALLOW
    assert d.category == Category.BULK_COMPUTE


def test_sbatch_burst_asks():
    p = std()
    d = p.classify_command("sbatch train.sh", work_dirs=WORK, recent_job_count=p.max_jobs_in_window)
    assert d.action == ASK
    assert d.severity == Severity.HIGH


def test_cloud_provision_command_asks():
    d = std().classify_command("gcloud compute instances create vm1 --zone us", work_dirs=WORK)
    assert d.action == ASK
    assert d.category == Category.BULK_COMPUTE


# ── credentials ────────────────────────────────────────────

def test_request_secret_asks():
    d = std().classify_command("ark-request-secret HF_TOKEN download-dataset", work_dirs=WORK)
    assert d.action == ASK
    assert d.category == Category.CREDENTIALS


def test_reading_aws_credentials_asks():
    d = std().classify_command("cat /home/u/.aws/credentials", work_dirs=WORK)
    assert d.action == ASK
    assert d.category == Category.CREDENTIALS


def test_hardcoded_secret_asks_and_is_redacted():
    d = std().classify_command("export HF_TOKEN=hf_abcdef123456 && python x.py", work_dirs=WORK)
    assert d.action == ASK
    assert d.category == Category.CREDENTIALS
    assert "hf_abcdef123456" not in d.detail   # value scrubbed
    assert "***" in d.detail


def test_redact_secrets_helper():
    from ark.intervention.policy import redact_secrets
    out = redact_secrets("OPENAI_API_KEY=sk-abc123def python run.py")
    assert "sk-abc123def" not in out
    assert "OPENAI_API_KEY=***" in out


# ── data_exfil ─────────────────────────────────────────────

def test_scp_to_remote_asks():
    d = std().classify_command("scp data.zip user@1.2.3.4:/tmp/", work_dirs=WORK)
    assert d.action == ASK
    assert d.category == Category.DATA_EXFIL


def test_scp_to_localhost_allowed():
    d = std().classify_command("scp a.txt localhost:/tmp/", work_dirs=WORK)
    assert d.action == ALLOW


def test_git_push_asks():
    d = std().classify_command("git push origin main", work_dirs=WORK)
    assert d.action == ASK
    assert d.category == Category.DATA_EXFIL


def test_normal_command_allowed():
    d = std().classify_command("python train.py --epochs 3", work_dirs=WORK)
    assert d.action == ALLOW


# ── autonomy levels ────────────────────────────────────────

def test_cautious_asks_on_low_severity_single_delete():
    d = InterventionPolicy(autonomy="cautious").classify_command(
        "rm /home/u/proj/results/tmp.txt", work_dirs=WORK)
    assert d.action == ASK  # cautious escalates even LOW


def test_autonomous_allows_high_but_asks_critical():
    p = InterventionPolicy(autonomy="autonomous")
    assert p.classify_command("rm -rf /home/u/proj/results/x", work_dirs=WORK).action == ALLOW
    assert p.classify_command("rm -rf .git", work_dirs=WORK).action == ASK


# ── category toggles ───────────────────────────────────────

def test_disabled_category_allows():
    p = InterventionPolicy(autonomy="standard", enabled={Category.CREDENTIALS})
    d = p.classify_command("rm -rf /home/u/proj/results/old", work_dirs=WORK)
    assert d.action == ALLOW
    assert d.category == Category.DESTRUCTIVE_FS  # still classified, just not gated


# ── structured orchestrator actions ────────────────────────

def test_action_cloud_provision_asks():
    d = std().classify_action("cloud_provision", provider="gcp", instance_type="a2-highgpu-1g")
    assert d.action == ASK
    assert d.category == Category.BULK_COMPUTE


def test_action_git_push_asks():
    d = std().classify_action("git_push", remote="origin", branch="main")
    assert d.action == ASK
    assert d.category == Category.DATA_EXFIL


def test_action_spend_notify_then_ask():
    p = InterventionPolicy(autonomy="standard", spend_notify_usd=20, spend_ask_usd=100)
    assert p.classify_action("spend", total_usd=5).action == ALLOW
    assert p.classify_action("spend", total_usd=25).action == NOTIFY
    assert p.classify_action("spend", total_usd=120).action == ASK


# ── config loading ─────────────────────────────────────────

def test_load_policy_defaults_on_empty():
    p = load_policy({})
    assert p.autonomy == "standard"
    assert p.enabled == set(Category.ALL)


def test_load_policy_reads_block():
    p = load_policy({"intervention": {
        "autonomy": "cautious",
        "categories": {"spend": False},
        "max_jobs_in_window": 3,
        "spend_ask_usd": 50,
    }})
    assert p.autonomy == "cautious"
    assert Category.SPEND not in p.enabled
    assert p.max_jobs_in_window == 3
    assert p.spend_ask_usd == 50.0


def test_load_policy_bad_autonomy_falls_back():
    assert load_policy({"intervention": {"autonomy": "yolo"}}).autonomy == "standard"
