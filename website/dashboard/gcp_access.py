"""Onboarding-time GCP access checks for the SkyPilot workspaces model.

In this model users don't hand us a service-account key — they grant the central
"ark-launcher" service account (scripts/setup_ark_launcher_sa.sh) access to their
own GCP project via IAM. That grant is the one thing that can silently be missing,
so onboarding verifies it: we probe the user's project *as the launcher SA* and
fail loudly if the SA can't see it.

Kept dependency-light: uses the already-installed google-api-python-client +
application-default credentials (which resolve to the launcher SA when
GOOGLE_APPLICATION_CREDENTIALS points at its key). No SkyPilot import.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger("website.dashboard")

# The roles a user must grant the launcher SA on their project — SkyPilot's
# "medium permissions" set (docs.skypilot.co GCP admin guide). Shown in the
# onboarding instructions and mirrored by scripts/setup_ark_launcher_sa.sh.
REQUIRED_ROLES = [
    "roles/browser",
    "roles/compute.admin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/storage.admin",
    "roles/iam.securityAdmin",
]


# Fixed local part of the launcher SA, matching scripts/setup_ark_launcher_sa.sh
# (``SA_ID="ark-launcher"``). Used to derive the email from the central project
# when no explicit setting / key file is present.
LAUNCHER_SA_ID = "ark-launcher"


def launcher_sa_email(settings=None) -> str:
    """Email of the central launcher SA, for the grant instructions.

    Resolution order (first hit wins):
      1. the explicit ``CLOUD_LAUNCHER_SA`` setting;
      2. ``client_email`` from the key file at ``GOOGLE_APPLICATION_CREDENTIALS``;
      3. derived as ``ark-launcher@<central-project>.iam.gserviceaccount.com``
         from ``CLOUD_GCP_PROJECT`` (the setup script builds the address exactly
         this way), so onboarding instructions still resolve on hosts that have
         the central project configured but no SA key mounted.

    Empty string only if none of those are available — the UI then shows a
    configuration hint instead of a bad address."""
    if settings is not None and getattr(settings, "cloud_launcher_sa", ""):
        return settings.cloud_launcher_sa
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if cred_path and os.path.exists(os.path.expanduser(cred_path)):
        try:
            with open(os.path.expanduser(cred_path)) as fh:
                email = json.load(fh).get("client_email", "") or ""
                if email:
                    return email
        except (OSError, ValueError):
            pass
    central = getattr(settings, "cloud_gcp_project", "") if settings is not None else ""
    if central:
        return f"{LAUNCHER_SA_ID}@{central}.iam.gserviceaccount.com"
    return ""


def launcher_org_customer_id(settings=None) -> str:
    """Directory customer id (e.g. ``C0abc1234``) of the org that owns the launcher
    SA, for Domain Restricted Sharing allowlists in the grant instructions.

    Empty string if not configured — the grant script then emits a discovery
    helper (``gcloud organizations list``) instead of a concrete value, since a
    user in the same directory as the launcher can look it up themselves."""
    if settings is not None:
        return getattr(settings, "cloud_launcher_org_customer_id", "") or ""
    return ""


def ensure_launcher_credentials(settings) -> dict:
    """Make the webapp process launch AS the central launcher SA, and report what
    identity it will actually use. Returns ``{ok, identity, is_service_account,
    source, detail}``.

    SkyPilot's SDK authenticates via Application Default Credentials
    (``google.auth.default()``), NOT the ``gcloud`` active account — so activating
    the SA in gcloud is not enough. If ``GOOGLE_APPLICATION_CREDENTIALS`` is unset
    we point it at the configured launcher key (``cloud_launcher_sa_key``) before
    any SkyPilot import. Then we resolve the identity and flag loudly if it is a
    *user* account: the per-user cross-project IAM grants are to the SA, so a user
    identity would fail auth on every tenant project."""
    source = "GOOGLE_APPLICATION_CREDENTIALS"
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        key = os.path.expanduser(getattr(settings, "cloud_launcher_sa_key", "") or "")
        if key and os.path.exists(key):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key
            source = f"cloud_launcher_sa_key ({key})"
        else:
            logger.warning(
                "No GOOGLE_APPLICATION_CREDENTIALS and no launcher SA key at %r — "
                "SkyPilot launches will use the host's ambient ADC (likely a user "
                "account), which lacks the per-tenant grants. Run "
                "scripts/setup_ark_launcher_sa.sh or set GOOGLE_APPLICATION_CREDENTIALS.",
                key or "(unset)")
            return {"ok": False, "identity": None, "is_service_account": False,
                    "source": None, "detail": "No launcher credentials configured."}

    try:
        import google.auth
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except Exception as exc:
        logger.warning("Could not resolve launcher ADC: %s", exc)
        return {"ok": False, "identity": None, "is_service_account": False,
                "source": source, "detail": str(exc)}

    sa_email = getattr(creds, "service_account_email", None)
    is_sa = bool(sa_email) and sa_email != "default"
    if is_sa:
        logger.info("SkyPilot launches will run as service account %s (via %s).",
                    sa_email, source)
        return {"ok": True, "identity": sa_email, "is_service_account": True,
                "source": source, "detail": f"Launching as {sa_email}."}
    # Not a service account → user creds. This provisions into tenant projects the
    # user account can't reach; make it impossible to miss.
    logger.warning(
        "SkyPilot launches would run as a USER account (%s), NOT the launcher SA. "
        "Per-tenant launches will fail auth. Set GOOGLE_APPLICATION_CREDENTIALS to "
        "the launcher SA key.", type(creds).__name__)
    return {"ok": False, "identity": type(creds).__name__, "is_service_account": False,
            "source": source, "detail": "Resolved to a user account, not the SA."}


def verify_project_access(project_id: str) -> dict:
    """Probe ``project_id`` as the launcher SA. Returns ``{ok, detail}``.

    ``ok`` is True only if the SA can read the project (i.e. the IAM grant is in
    place). A 403 means the grant is missing — the message tells the user exactly
    what to do. Any other failure (bad project id, API disabled, no ADC) also
    returns ``ok: False`` with the reason, so onboarding never *looks* verified
    when it isn't."""
    if not project_id:
        return {"ok": False, "detail": "No GCP project id set."}
    try:
        import google.auth
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError as exc:
        return {"ok": False,
                "detail": f"GCP client libraries unavailable on the server: {exc}"}

    try:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except Exception as exc:  # no ADC / launcher SA not configured on the host
        return {"ok": False,
                "detail": f"Server has no GCP credentials configured: {exc}"}

    try:
        crm = build("cloudresourcemanager", "v1", credentials=creds,
                    cache_discovery=False)
        proj = crm.projects().get(projectId=project_id).execute()
        state = proj.get("lifecycleState", "UNKNOWN")
        if state != "ACTIVE":
            return {"ok": False,
                    "detail": f"Project '{project_id}' is not ACTIVE (state={state})."}
        return {"ok": True,
                "detail": f"Access confirmed — the launcher can provision into "
                          f"'{project_id}'."}
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        sa = launcher_sa_email() or "the launcher service account"
        if status == 403:
            return {"ok": False,
                    "detail": (f"Access denied to '{project_id}'. Grant {sa} the "
                               f"required roles on your project, then retry.")}
        if status == 404:
            return {"ok": False,
                    "detail": f"Project '{project_id}' not found — check the id."}
        return {"ok": False, "detail": f"GCP error checking '{project_id}': {exc}"}
    except Exception as exc:
        return {"ok": False, "detail": f"Could not verify '{project_id}': {exc}"}
