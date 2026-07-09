"""Onboarding-time AWS access checks for the SkyPilot workspaces model.

The AWS analog of gcp_access.py. AWS has no "projects" — isolation is per
*account*, and the equivalent of the GCP cross-project IAM grant is a cross-
account STS AssumeRole. So in this model users don't hand us an access key: they
create an "ark-launcher" IAM **role** in their own account whose trust policy
names our central launcher identity (scripts/setup_ark_launcher_aws.sh) and whose
permissions policy carries SkyPilot's EC2/IAM/S3 rights. We then reach their
account by assuming that role.

That trust grant is the one thing that can silently be missing, so onboarding
verifies it: we try to *assume the tenant role as the launcher* and fail loudly
if we can't. Kept dependency-light: boto3 is imported lazily (like googleapiclient
in gcp_access) so importing this module never requires the AWS SDK, and the
webapp still boots on a host without it.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("website.dashboard")

# The AWS-managed policies a user must attach to the tenant role — SkyPilot's
# documented AWS permission set (docs.skypilot.co AWS admin guide). Shown in the
# onboarding instructions and mirrored by scripts/setup_ark_launcher_aws.sh. Kept
# as friendly names; the grant script/UI build the full policy ARNs
# (arn:aws:iam::aws:policy/<name>).
REQUIRED_POLICIES = [
    "AmazonEC2FullAccess",
    "IAMFullAccess",
    "AmazonS3FullAccess",
]

# Fixed name of the role the user creates in their account, matching
# scripts/setup_ark_launcher_aws.sh. The tenant role ARN is derived from the
# user's account id + this name, so we never store a per-user ARN.
TENANT_ROLE_NAME = "ark-launcher"

# STS is global but boto3 clients still want a region; fall back to this when the
# operator has configured none.
_DEFAULT_REGION = "us-east-1"

_ACCOUNT_RE = re.compile(r"^\d{12}$")


def _region(settings) -> str:
    return (getattr(settings, "cloud_aws_region", "") or "").strip() or _DEFAULT_REGION


def tenant_role_arn(account_id: str) -> str:
    """Derive the ARN of the tenant's ark-launcher role from their account id.

    Empty string for a malformed id (not 12 digits) so callers surface a clear
    "check the account id" instead of assembling a bogus ARN."""
    aid = (account_id or "").strip()
    if not _ACCOUNT_RE.match(aid):
        return ""
    return f"arn:aws:iam::{aid}:role/{TENANT_ROLE_NAME}"


def launcher_external_id(settings=None) -> str:
    """Optional STS ExternalId embedded in the trust policy + used on assume.

    Empty string ⇒ omitted from both (the grant script leaves the condition out)."""
    if settings is not None:
        return getattr(settings, "cloud_launcher_aws_external_id", "") or ""
    return ""


def _base_session(settings):
    """A boto3 Session for the CENTRAL launcher identity.

    Resolution mirrors ensure_launcher_credentials' "support both" intent:
      * ``cloud_launcher_aws_credential_source`` set (Ec2InstanceMetadata /
        Environment) ⇒ the host's own role/env creds (no stored key);
      * else the base ``cloud_launcher_aws_profile`` from ~/.aws;
      * else the default boto3 chain.
    """
    import boto3  # noqa: WPS433 (intentional lazy import, mirrors gcp_access)

    region = _region(settings)
    cred_source = (getattr(settings, "cloud_launcher_aws_credential_source", "") or "").strip()
    if cred_source:
        # Host role / env — the default chain already resolves these.
        return boto3.Session(region_name=region)
    profile = (getattr(settings, "cloud_launcher_aws_profile", "") or "").strip()
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def launcher_role_arn(settings=None) -> str:
    """ARN of the central launcher identity, for the trust-policy instructions.

    Resolution order (first hit wins):
      1. the explicit ``CLOUD_LAUNCHER_ROLE_ARN`` setting;
      2. the caller identity of the base launcher creds (``sts:GetCallerIdentity``)
         — so instructions resolve on a host configured with the launcher profile
         but no explicit ARN set.

    Empty string only if neither is available — the UI then shows a configuration
    hint instead of a bad ARN."""
    if settings is not None and getattr(settings, "cloud_launcher_role_arn", ""):
        return settings.cloud_launcher_role_arn
    try:
        sess = _base_session(settings)
        return sess.client("sts").get_caller_identity().get("Arn", "") or ""
    except Exception:  # no creds / no boto3 — fall back to the config hint
        return ""


def ensure_launcher_credentials(settings) -> dict:
    """Resolve the central launcher creds and report the identity we'll assume as.

    Returns ``{ok, identity, source, detail}``. The AWS analog of the GCP check:
    unlike GCP (where a *user* ADC would silently fail), any resolvable identity
    with ``sts:AssumeRole`` works here, so ``ok`` just means "we have usable
    launcher credentials". Fails loudly (``ok: False``) when nothing resolves,
    since every per-tenant assume would then fail."""
    cred_source = (getattr(settings, "cloud_launcher_aws_credential_source", "") or "").strip()
    profile = (getattr(settings, "cloud_launcher_aws_profile", "") or "").strip()
    source = cred_source or (f"profile {profile}" if profile else "default credential chain")
    try:
        sess = _base_session(settings)
        ident = sess.client("sts").get_caller_identity()
    except ImportError as exc:
        return {"ok": False, "identity": None, "source": source,
                "detail": f"AWS SDK (boto3) unavailable on the server: {exc}"}
    except Exception as exc:
        logger.warning(
            "No usable AWS launcher credentials (source=%s): %s — SkyPilot AWS "
            "launches will fail. Run scripts/setup_ark_launcher_aws.sh or set "
            "CLOUD_LAUNCHER_AWS_CREDENTIAL_SOURCE.", source, exc)
        return {"ok": False, "identity": None, "source": source, "detail": str(exc)}
    arn = ident.get("Arn", "")
    logger.info("SkyPilot AWS launches will assume tenant roles as %s (via %s).",
                arn, source)
    return {"ok": True, "identity": arn, "source": source,
            "detail": f"Launching as {arn}."}


def verify_account_access(account_id: str, settings=None) -> dict:
    """Probe the user's account by assuming their ark-launcher role. Returns
    ``{ok, detail}``.

    ``ok`` is True only if the launcher can assume the tenant role (i.e. the trust
    grant is in place). An AccessDenied means the trust policy is missing or wrong
    — the message tells the user what to fix. Any other failure (bad account id,
    no launcher creds, no boto3) also returns ``ok: False`` with the reason, so
    onboarding never *looks* verified when it isn't."""
    arn = tenant_role_arn(account_id)
    if not arn:
        return {"ok": False,
                "detail": "Enter your 12-digit AWS account id (digits only)."}
    try:
        from botocore.exceptions import ClientError  # noqa: WPS433 (lazy)
    except ImportError as exc:
        return {"ok": False,
                "detail": f"AWS SDK (boto3) unavailable on the server: {exc}"}

    try:
        sess = _base_session(settings)
        sts = sess.client("sts")
    except Exception as exc:  # no launcher creds configured on the host
        return {"ok": False,
                "detail": f"Server has no AWS launcher credentials configured: {exc}"}

    kwargs = {"RoleArn": arn, "RoleSessionName": "ark-onboarding-verify"}
    ext = launcher_external_id(settings)
    if ext:
        kwargs["ExternalId"] = ext
    try:
        sts.assume_role(**kwargs)
        return {"ok": True,
                "detail": f"Access confirmed — the launcher can provision into "
                          f"account {account_id}."}
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        who = launcher_role_arn(settings) or "the launcher identity"
        if code in ("AccessDenied", "AccessDeniedException"):
            return {"ok": False,
                    "detail": (f"Access denied assuming {arn}. Make sure the "
                               f"'{TENANT_ROLE_NAME}' role exists in account "
                               f"{account_id} and its trust policy allows {who}, "
                               f"then retry.")}
        if code in ("NoSuchEntity", "MalformedPolicyDocument"):
            return {"ok": False,
                    "detail": f"Role '{TENANT_ROLE_NAME}' not found in account "
                              f"{account_id} — run the setup, then retry."}
        return {"ok": False, "detail": f"AWS error checking account {account_id}: {exc}"}
    except Exception as exc:
        return {"ok": False, "detail": f"Could not verify account {account_id}: {exc}"}
