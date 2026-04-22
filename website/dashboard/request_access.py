"""Public /api/request-access endpoint: invite-only beta access requests.

Unprotected by CF Access (registered on the outer FastAPI BEFORE the
/dashboard mount). Sends email to contact@idea2paper.org via the same SMTP
path as magic-link login. Rate-limited per IP to resist abuse.
"""

from __future__ import annotations

import logging
import re
import smtplib
import time
from email.mime.text import MIMEText

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from .config import get_settings

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

logger = logging.getLogger(__name__)

router = APIRouter()

RECIPIENT = "contact@idea2paper.org"
COOLDOWN_SECONDS = 60
MAX_PURPOSE_LEN = 2000
MAX_NAME_LEN = 200

_last_submit: dict[str, float] = {}


class AccessRequest(BaseModel):
    email: str
    name: str | None = ""
    affiliation: str | None = ""
    purpose: str | None = ""

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("Invalid email address")
        return v


def _client_ip(request: Request) -> str:
    # CF Tunnel forwards real IP in CF-Connecting-IP; fall back to socket peer.
    return (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )


def _sanitize(s: str, limit: int) -> str:
    # Strip control chars (including \r\n) that could be injected into headers
    return re.sub(r"[\x00-\x1f\x7f]+", " ", (s or "").strip())[:limit]


@router.post("/api/request-access")
async def request_access(payload: AccessRequest, request: Request):
    ip = _client_ip(request)
    now = time.time()
    last = _last_submit.get(ip, 0)
    if now - last < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last))
        raise HTTPException(429, f"Please wait {wait}s before sending another request.")

    settings = get_settings()
    if not (settings.smtp_user and settings.smtp_password) and not settings.smtp_relay:
        logger.error("Access request received but SMTP is not configured.")
        raise HTTPException(500, "Email service is not configured. Please email contact@idea2paper.org directly.")

    email = str(payload.email).strip().lower()
    name = _sanitize(payload.name or "", MAX_NAME_LEN)
    affiliation = _sanitize(payload.affiliation or "", MAX_NAME_LEN)
    purpose = _sanitize(payload.purpose or "", MAX_PURPOSE_LEN)

    subject = f"[ARK] Access request from {email}"
    body = (
        f"A new access request was submitted via idea2paper.org/request-access:\n\n"
        f"  Email:       {email}\n"
        f"  Name:        {name or '(not provided)'}\n"
        f"  Affiliation: {affiliation or '(not provided)'}\n"
        f"  Source IP:   {ip}\n\n"
        f"Purpose:\n{purpose or '(not provided)'}\n\n"
        f"---\nReply directly to this email to respond to the requester.\n"
    )

    from_addr = getattr(settings, "smtp_from", "") or RECIPIENT
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = from_addr
    msg["To"] = RECIPIENT
    msg["Subject"] = subject
    msg["Reply-To"] = email

    sent = False
    relay = getattr(settings, "smtp_relay", "")
    if relay:
        try:
            with smtplib.SMTP(relay, 25, timeout=10) as server:
                server.sendmail(from_addr, [RECIPIENT], msg.as_string())
            sent = True
        except Exception as e:
            logger.warning(f"Relay send failed ({e}), trying auth SMTP…")

    if not sent and settings.smtp_user and settings.smtp_password:
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(from_addr, [RECIPIENT], msg.as_string())
            sent = True
        except Exception as e:
            logger.error(f"Access request email send failed: {e}")

    if not sent:
        raise HTTPException(500, "Could not send email. Please try again later or email contact@idea2paper.org.")

    _last_submit[ip] = now
    logger.info(f"Access request sent from {email} (ip={ip})")
    return {"ok": True, "message": "Thanks — your request was sent. We'll be in touch."}
