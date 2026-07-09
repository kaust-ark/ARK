"""Per-user AWS isolation for SkyPilot (multi-tenant, SKYPILOT_PLAN.md).

The AWS analog of test_skypilot_workspaces.py. AWS has no cross-project grant —
isolation is per account via cross-account STS AssumeRole. A user creates an
``ark-launcher`` role in their account trusting the central launcher; a per-user
``ws-<id>`` ~/.aws profile assumes it, and the SkyPilot workspace pins that profile.

Locked here:
- ``aws_access`` — tenant role ARN derivation + the assume-role verify probe
  (boto3 is faked; it isn't installed in CI, and the module imports it lazily).
- ``skyworkspaces`` — the AWS block in ``build_workspaces`` and the managed
  ``[profile ws-*]`` slice of ~/.aws/config (``render_aws_profiles``).
- ``routes._resolve_compute_config`` — the ``skypilot:aws`` orchestrator shaping.
"""

import sys
import types

import pytest

from website.dashboard import aws_access


# ── fake boto3 / botocore (not installed; imported lazily by aws_access) ──────
def _install_fake_boto(monkeypatch, *, caller_arn="arn:aws:iam::111111111111:user/ark-launcher",
                       assume_error=None, record=None):
    """Install minimal fake ``boto3`` + ``botocore.exceptions`` modules. ``record``
    (a dict) captures the last assume_role kwargs and the Session kwargs so tests
    can assert ExternalId / profile plumbing."""
    record = record if record is not None else {}

    class ClientError(Exception):
        def __init__(self, code):
            self.response = {"Error": {"Code": code}}
            super().__init__(code)

    class _FakeSTS:
        def get_caller_identity(self):
            return {"Arn": caller_arn, "Account": caller_arn.split(":")[4]}

        def assume_role(self, **kw):
            record["assume_kwargs"] = kw
            if assume_error:
                raise ClientError(assume_error)
            return {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK",
                                    "SessionToken": "ST"}}

    class _FakeSession:
        def __init__(self, **kw):
            record["session_kwargs"] = kw

        def client(self, name):
            assert name == "sts"
            return _FakeSTS()

    boto3 = types.ModuleType("boto3")
    boto3.Session = _FakeSession
    botocore = types.ModuleType("botocore")
    exc = types.ModuleType("botocore.exceptions")
    exc.ClientError = ClientError
    botocore.exceptions = exc
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exc)
    return record


def _settings(**kw):
    base = dict(cloud_launcher_role_arn="", cloud_aws_region="us-east-1",
                cloud_launcher_aws_profile="ark-launcher",
                cloud_launcher_aws_credential_source="",
                cloud_launcher_aws_external_id="")
    base.update(kw)
    return types.SimpleNamespace(**base)


# ── tenant role ARN derivation (pure) ────────────────────────────────────────
def test_tenant_role_arn_from_account():
    assert aws_access.tenant_role_arn("123456789012") == (
        "arn:aws:iam::123456789012:role/ark-launcher")


@pytest.mark.parametrize("bad", ["", "12345", "abcdefghijkl", "1234567890123", None])
def test_tenant_role_arn_rejects_malformed(bad):
    assert aws_access.tenant_role_arn(bad) == ""


# ── launcher identity resolution ─────────────────────────────────────────────
def test_launcher_role_arn_prefers_explicit_setting(monkeypatch):
    _install_fake_boto(monkeypatch, caller_arn="arn:aws:iam::999:user/derived")
    s = _settings(cloud_launcher_role_arn="arn:aws:iam::111:role/explicit")
    assert aws_access.launcher_role_arn(s) == "arn:aws:iam::111:role/explicit"


def test_launcher_role_arn_falls_back_to_caller_identity(monkeypatch):
    _install_fake_boto(monkeypatch, caller_arn="arn:aws:iam::222:user/ark-launcher")
    assert aws_access.launcher_role_arn(_settings()) == "arn:aws:iam::222:user/ark-launcher"


def test_launcher_role_arn_empty_when_no_creds(monkeypatch):
    # No boto3 available at all → the caller-identity probe fails, returns "".
    monkeypatch.setitem(sys.modules, "boto3", None)  # import boto3 → ImportError
    assert aws_access.launcher_role_arn(_settings()) == ""


# ── verify (assume-role probe) ───────────────────────────────────────────────
def test_verify_ok_on_successful_assume(monkeypatch):
    rec = _install_fake_boto(monkeypatch)
    out = aws_access.verify_account_access("123456789012", _settings())
    assert out["ok"] is True
    assert "123456789012" in out["detail"]
    # No external id configured ⇒ not sent on assume.
    assert "ExternalId" not in rec["assume_kwargs"]
    assert rec["assume_kwargs"]["RoleArn"] == "arn:aws:iam::123456789012:role/ark-launcher"


def test_verify_passes_external_id_when_configured(monkeypatch):
    rec = _install_fake_boto(monkeypatch)
    out = aws_access.verify_account_access(
        "123456789012", _settings(cloud_launcher_aws_external_id="secret-xid"))
    assert out["ok"] is True
    assert rec["assume_kwargs"]["ExternalId"] == "secret-xid"


def test_verify_uses_base_profile_session(monkeypatch):
    rec = _install_fake_boto(monkeypatch)
    aws_access.verify_account_access("123456789012", _settings())
    assert rec["session_kwargs"].get("profile_name") == "ark-launcher"


def test_verify_credential_source_skips_profile(monkeypatch):
    rec = _install_fake_boto(monkeypatch)
    aws_access.verify_account_access(
        "123456789012", _settings(cloud_launcher_aws_credential_source="Ec2InstanceMetadata"))
    # Host role → the default chain, no profile_name pinned.
    assert "profile_name" not in rec["session_kwargs"]


def test_verify_access_denied_is_actionable(monkeypatch):
    _install_fake_boto(monkeypatch, assume_error="AccessDenied")
    out = aws_access.verify_account_access("123456789012", _settings())
    assert out["ok"] is False
    assert "trust policy" in out["detail"].lower() or "allow" in out["detail"].lower()


def test_verify_rejects_bad_account_before_calling_aws(monkeypatch):
    # No boto3 installed at all; a malformed id must fail fast without importing it.
    out = aws_access.verify_account_access("nope", _settings())
    assert out["ok"] is False
    assert "12-digit" in out["detail"]


# ── build_workspaces: AWS block ──────────────────────────────────────────────
def _user(uid):
    return types.SimpleNamespace(id=uid)


def test_build_workspaces_includes_aws_and_combines_with_gcp():
    from website.dashboard.skyworkspaces import build_workspaces, workspace_name_for

    keys = {
        "alice": {"aws_account_id": "123456789012"},
        "bob": {"gcp_project": "proj-b", "aws_account_id": "210987654321"},
        "carol": {},
    }
    users = [_user("alice"), _user("bob"), _user("carol")]
    ws = build_workspaces(users, lambda u: keys[u.id])

    assert ws[workspace_name_for("alice")] == {"aws": {"profile": workspace_name_for("alice")}}
    # A user with both clouds gets both blocks in one workspace.
    assert ws[workspace_name_for("bob")] == {
        "gcp": {"project_id": "proj-b"},
        "aws": {"profile": workspace_name_for("bob")},
    }
    assert workspace_name_for("carol") not in ws  # neither cloud ⇒ omitted


# ── render_aws_profiles: managed [profile ws-*] slice ────────────────────────
def test_render_aws_profiles_preserves_unmanaged_and_drops_stale(tmp_path, monkeypatch):
    import configparser
    from website.dashboard import skyworkspaces as sw

    cfg = tmp_path / "aws_config"
    cfg.write_text(
        "[default]\nregion = eu-west-1\n\n"
        "[profile hand-authored]\nregion = us-west-2\n\n"
        "[profile ws-OLD]\nrole_arn = arn:aws:iam::000000000000:role/ark-launcher\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))

    keys = {"alice": {"aws_account_id": "123456789012", "aws_region": "us-east-2"},
            "carol": {}}
    users = [_user("alice"), _user("carol")]
    monkeypatch.setattr("website.dashboard.db.get_session",
                        lambda *a, **k: __import__("contextlib").nullcontext(None))
    settings = _settings()

    n = sw.render_aws_profiles("unused", get_user_keys=lambda u: keys[u.id],
                               list_users=lambda _s: users, settings=settings)
    assert n == 1

    parsed = configparser.ConfigParser()
    parsed.read(cfg)
    assert parsed["default"]["region"] == "eu-west-1"              # unmanaged preserved
    assert parsed["profile hand-authored"]["region"] == "us-west-2"  # hand-authored preserved
    assert "profile ws-OLD" not in parsed                          # stale managed dropped
    assert "profile ws-carol" not in parsed                        # account-less omitted
    body = parsed["profile ws-alice"]
    assert body["role_arn"] == "arn:aws:iam::123456789012:role/ark-launcher"
    assert body["region"] == "us-east-2"                           # per-user region wins
    assert body["source_profile"] == "ark-launcher"


def test_render_aws_profiles_credential_source_over_profile(tmp_path, monkeypatch):
    import configparser
    from website.dashboard import skyworkspaces as sw

    cfg = tmp_path / "aws_config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    keys = {"alice": {"aws_account_id": "123456789012"}}
    monkeypatch.setattr("website.dashboard.db.get_session",
                        lambda *a, **k: __import__("contextlib").nullcontext(None))
    settings = _settings(cloud_launcher_aws_credential_source="Ec2InstanceMetadata",
                         cloud_launcher_aws_external_id="xid")

    sw.render_aws_profiles("unused", get_user_keys=lambda u: keys[u.id],
                           list_users=lambda _s: [_user("alice")], settings=settings)
    parsed = configparser.ConfigParser()
    parsed.read(cfg)
    body = parsed["profile ws-alice"]
    assert body["credential_source"] == "Ec2InstanceMetadata"
    assert "source_profile" not in body
    assert body["external_id"] == "xid"
    assert body["region"] == "us-east-1"  # falls back to operator default


# ── routes: skypilot:aws orchestrator shaping ────────────────────────────────
def _project(orch_backend):
    return types.SimpleNamespace(
        name="proj", title="T", idea="an idea", venue="NeurIPS",
        venue_format="conference", venue_pages=9, layout_mode="balanced",
        mode="paper", max_iterations=3, max_dev_iterations=3,
        figure_generation="nano_banana", orchestrator_compute_backend=orch_backend,
        experiment_compute_backend="local", compute_backend="local",
        telegram_token=None, telegram_chat_id=None, skip_deep_research=False,
    )


def test_orchestrator_aws_shapes_region_workspace_and_no_gcp_bits(tmp_path, monkeypatch):
    """A skypilot:aws orchestrator pins the user's region + per-user workspace,
    and omits the GCP-only instance_type / image_id (stock AMI + full setup)."""
    import yaml
    from website.dashboard import routes
    from website.dashboard.skyworkspaces import workspace_name_for

    user = types.SimpleNamespace(id="alice")
    monkeypatch.setattr(routes, "_get_user_keys",
                        lambda u: {"aws_account_id": "123456789012", "aws_region": "eu-central-1"})
    settings = types.SimpleNamespace(
        cloud_gcp_project="central-proj", cloud_conda_env="ark-base",
        cloud_aws_region="us-east-1")

    routes._write_config_yaml(tmp_path, _project("skypilot:aws"), user, settings)
    occ = yaml.safe_load((tmp_path / "config.yaml").read_text())["orchestrator_compute_backend"]

    assert occ["type"] == "skypilot"
    assert occ["cloud"] == "aws"
    assert occ["region"] == "eu-central-1"              # per-user region wins
    assert occ["workspace"] == workspace_name_for("alice")
    assert "instance_type" not in occ                   # n4-standard-2 is GCP-only
    assert "image_id" not in occ                        # no baked AMI yet
    assert occ["setup_commands"]                        # full install on stock AMI


def test_orchestrator_aws_region_falls_back_to_operator_default(tmp_path, monkeypatch):
    import yaml
    from website.dashboard import routes

    user = types.SimpleNamespace(id="alice")
    monkeypatch.setattr(routes, "_get_user_keys", lambda u: {"aws_account_id": "123456789012"})
    settings = types.SimpleNamespace(
        cloud_gcp_project="", cloud_conda_env="ark-base", cloud_aws_region="ap-south-1")

    routes._write_config_yaml(tmp_path, _project("skypilot:aws"), user, settings)
    occ = yaml.safe_load((tmp_path / "config.yaml").read_text())["orchestrator_compute_backend"]
    assert occ["region"] == "ap-south-1"
