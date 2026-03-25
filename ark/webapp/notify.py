"""Email notifications via smtplib (with sendmail fallback), plus Telegram."""

from __future__ import annotations

import json
import logging
import shutil
import smtplib
import subprocess
import urllib.request
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger("ark.webapp.notify")

# ── Telegram ──────────────────────────────────────────────────────────────────

_TELEGRAM_CONFIG = Path.home() / ".ark" / "telegram.yaml"


def _telegram_creds():
    if not _TELEGRAM_CONFIG.exists():
        return None, None
    import yaml
    cfg = yaml.safe_load(_TELEGRAM_CONFIG.read_text()) or {}
    return cfg.get("bot_token"), cfg.get("chat_id")


def send_telegram_notify(text: str, bot_token: str = None, chat_id: str = None) -> bool:
    """Send a Telegram message. Returns True on success."""
    token = bot_token or None
    if not token or not chat_id:
        return False
    payload = json.dumps({"chat_id": chat_id, "text": text,
                          "parse_mode": "HTML", "disable_web_page_preview": True})
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        logger.warning(f"Telegram notify failed: {e}")
        return False


def send_telegram_login_link(link: str) -> bool:
    """Send a magic login link via Telegram. Returns True on success."""
    return send_telegram_notify(
        f"🔑 <b>ARK login link</b>\n<a href='{link}'>{link}</a>\nExpires in 1 hour."
    )


# ── Email ─────────────────────────────────────────────────────────────────────

def send_completion_email(
    settings,
    to_email: str,
    project_name: str,
    score: float,
    pdf_path: str | None,
    project_url: str,
) -> bool:
    """Send a project completion email. Returns True on success."""
    if not (settings.smtp_user and settings.smtp_password):
        logger.warning("SMTP credentials not configured — skipping email.")
        return False

    subject = f"[ARK] '{project_name}' finished — score {score:.1f}/10"

    body = f"""\
Your ARK research project <b>{project_name}</b> has finished!

<ul>
  <li><b>Score:</b> {score:.1f} / 10</li>
  <li><b>Dashboard:</b> <a href="{project_url}">{project_url}</a></li>
</ul>

{"<p>The PDF is attached below.</p>" if pdf_path else ""}

<p>— ARK Automatic Research Kit</p>
"""

    import socket as _socket
    from_addr = f"ark@{_socket.gethostname()}.kaust.edu.sa"
    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))

    # Attach PDF if it exists
    if pdf_path:
        pdf_file = Path(pdf_path)
        if pdf_file.exists():
            with open(pdf_file, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{pdf_file.name}"',
            )
            msg.attach(part)

    relay = "ciuxrelay.kaust.edu.sa"
    try:
        with smtplib.SMTP(relay, 25, timeout=10) as server:
            server.sendmail(from_addr, to_email, msg.as_string())
        logger.info(f"Completion email sent to {to_email} for project '{project_name}'")
        return True
    except Exception as e:
        logger.warning(f"Relay failed ({e}), trying SMTP auth…")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(from_addr, to_email, msg.as_string())
        logger.info(f"Completion email sent to {to_email} for project '{project_name}'")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


def _sendmail_fallback(from_addr: str, to_email: str, msg_str: str) -> bool:
    """Send via local sendmail binary. Returns True on success."""
    sendmail = shutil.which("sendmail")
    if not sendmail:
        return False
    try:
        proc = subprocess.run(
            [sendmail, "-f", from_addr, to_email],
            input=msg_str.encode(),
            capture_output=True,
            timeout=15,
        )
        if proc.returncode == 0:
            logger.info(f"Magic link sent via sendmail to {to_email}")
            return True
        logger.error(f"sendmail exited {proc.returncode}: {proc.stderr.decode()}")
        return False
    except Exception as e:
        logger.error(f"sendmail failed: {e}")
        return False


def send_magic_link_email(settings, to_email: str, link: str) -> bool:
    """Send a magic login link email. Returns True on success."""
    subject = "[ARK] Your login link"
    body = f"""\
<p>Click the link below to sign in to ARK Research Portal:</p>
<p><a href="{link}">{link}</a></p>
<p>This link expires in 1 hour.</p>
<p>If you did not request this, ignore this email.</p>
<p>— ARK Automatic Research Kit</p>
"""
    import socket as _socket
    relay_domain = getattr(settings, "smtp_relay", "") or "ciuxrelay.kaust.edu.sa"
    relay_domain = relay_domain.split(".", 1)[-1]  # ciuxrelay.kaust.edu.sa → kaust.edu.sa
    from_addr = f"ark@{_socket.gethostname()}.{relay_domain}"
    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))
    msg_str = msg.as_string()

    # Try KAUST relay (no auth, port 25) first if configured
    relay = getattr(settings, "smtp_relay", "") or "ciuxrelay.kaust.edu.sa"
    try:
        with smtplib.SMTP(relay, 25, timeout=10) as server:
            server.sendmail(from_addr, to_email, msg_str)
        logger.info(f"Magic link email sent via relay ({relay}) to {to_email}")
        return True
    except Exception as e:
        logger.warning(f"Relay failed ({e}), trying SMTP auth…")

    # Try SMTP with auth
    if settings.smtp_user and settings.smtp_password:
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(from_addr, to_email, msg_str)
            logger.info(f"Magic link email sent via SMTP to {to_email}")
            return True
        except Exception as e:
            logger.warning(f"SMTP failed ({e}), trying sendmail fallback…")

    # Fallback to local sendmail
    return _sendmail_fallback(from_addr, to_email, msg_str)
