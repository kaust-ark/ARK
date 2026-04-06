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

from ark.paths import get_config_dir

_TELEGRAM_CONFIG = None  # lazy


def _tg_config_path() -> Path:
    global _TELEGRAM_CONFIG
    if _TELEGRAM_CONFIG is None:
        _TELEGRAM_CONFIG = get_config_dir() / "telegram.yaml"
    return _TELEGRAM_CONFIG


def _telegram_creds():
    cfg_path = _tg_config_path()
    if not cfg_path.exists():
        return None, None
    import yaml
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
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


def send_welcome_email(settings, to_email: str, user_name: str, base_url: str) -> bool:
    """Send a one-time welcome email to a new ARK user. Returns True on success."""
    import socket as _socket
    from email.utils import formatdate, make_msgid
    from email.mime.image import MIMEImage

    logo_path = Path(__file__).parent / "static" / "logo_ark_transparent.png"

    subject = "Welcome to ARK — Automatic Research Kit"

    plain = f"""\
Hi {user_name},

Thanks for joining ARK -- an AI-powered platform that automates the
research pipeline from idea to polished paper.

What ARK does:
- End-to-end research automation: planning, coding, experiments, LaTeX
- Multi-venue support: NeurIPS, ICML, ACL, CVPR, IEEE, ACM and more
- Live dashboard: track progress, download PDFs, review scores

Get started: {base_url}

ARK is built by the research team at KAUST. We are actively iterating
and would love your feedback. Just reply to this email or reach out at
jihao.xin@kaust.edu.sa.

-- ARK Research Portal, KAUST
"""

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:#f0fdfa;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0fdfa;padding:32px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

  <!-- Banner -->
  <tr><td bgcolor="#0d9488" style="background:#0d9488;background:linear-gradient(135deg,#0d9488 0%,#134e4a 100%);padding:40px 40px 36px;text-align:center;">
    <table cellpadding="0" cellspacing="0" style="margin:0 auto 16px;"><tr>
      <td><img src="cid:ark_logo" alt="ARK" height="52" style="display:block;" /></td>
      <td style="padding-left:14px;color:#ccfbf1;font-size:38px;font-weight:300;letter-spacing:6px;vertical-align:middle;font-family:Georgia,'Times New Roman',serif;">ARK</td>
    </tr></table>
    <h1 style="margin:0;color:#ffffff;font-size:28px;font-weight:700;letter-spacing:-0.5px;">
      Welcome Onboard!
    </h1>
  </td></tr>

  <!-- Body -->
  <tr><td style="padding:40px 44px;">
    <p style="margin:0 0 20px;color:#1a1a1a;font-size:17px;line-height:1.6;">
      Hi <strong>{user_name}</strong>,
    </p>
    <p style="margin:0 0 20px;color:#333;font-size:15px;line-height:1.7;">
      Thanks for joining <strong>ARK</strong> &mdash; an AI-powered platform that automates
      the research pipeline from idea to polished paper. Upload an idea, pick a venue,
      and let ARK handle literature review, experimentation, writing, and compilation.
    </p>

    <!-- Feature highlights -->
    <table width="100%" cellpadding="0" cellspacing="0" style="margin:24px 0;">
      <tr>
        <td style="padding:12px 16px;background:#f0fdfa;border-radius:10px;border-left:4px solid #0d9488;">
          <p style="margin:0;color:#134e4a;font-size:14px;line-height:1.6;">
            <strong style="color:#0d9488;">Idea &rarr; Paper</strong><br/>
            End-to-end research automation: planning, coding, experiments, and LaTeX compilation.
          </p>
        </td>
      </tr>
      <tr><td style="height:10px;"></td></tr>
      <tr>
        <td style="padding:12px 16px;background:#f0fdfa;border-radius:10px;border-left:4px solid #0d9488;">
          <p style="margin:0;color:#134e4a;font-size:14px;line-height:1.6;">
            <strong style="color:#0d9488;">Multi-Venue Support</strong><br/>
            NeurIPS, ICML, ACL, IEEE, ACM &mdash; ARK formats for your target venue automatically.
          </p>
        </td>
      </tr>
      <tr><td style="height:10px;"></td></tr>
      <tr>
        <td style="padding:12px 16px;background:#f0fdfa;border-radius:10px;border-left:4px solid #0d9488;">
          <p style="margin:0;color:#134e4a;font-size:14px;line-height:1.6;">
            <strong style="color:#0d9488;">Live Dashboard</strong><br/>
            Track progress in real time, download PDFs, and review scores &mdash; all from the web portal.
          </p>
        </td>
      </tr>
    </table>

    <!-- CTA Button -->
    <table width="100%" cellpadding="0" cellspacing="0" style="margin:28px 0;">
      <tr><td align="center">
        <a href="{base_url}" style="display:inline-block;background:#0d9488;color:#fff;
           font-size:15px;font-weight:600;padding:14px 36px;border-radius:8px;
           text-decoration:none;letter-spacing:0.3px;">
          Open ARK Dashboard
        </a>
      </td></tr>
    </table>

    <!-- About us -->
    <p style="margin:0 0 16px;color:#333;font-size:15px;line-height:1.7;">
      ARK is built by the research team at
      <strong>King Abdullah University of Science and Technology (KAUST)</strong>.
      We&rsquo;re actively iterating on the platform and would love your feedback.
    </p>
    <p style="margin:0 0 8px;color:#333;font-size:15px;line-height:1.7;">
      Found a bug? Have an idea? Just reply to this email or reach out at
      <a href="mailto:jihao.xin@kaust.edu.sa" style="color:#0d9488;text-decoration:none;font-weight:600;">
        jihao.xin@kaust.edu.sa</a>.
    </p>
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f8fffe;padding:24px 44px;border-top:1px solid #e0f2f1;">
    <p style="margin:0;color:#999;font-size:12px;line-height:1.5;text-align:center;">
      ARK &mdash; Automatic Research Kit &bull; KAUST<br/>
      You received this email because you signed up for ARK.
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>
"""

    relay_domain = getattr(settings, "smtp_relay", "") or "ciuxrelay.kaust.edu.sa"
    relay_host = relay_domain
    relay_domain = relay_domain.split(".", 1)[-1]
    from_addr = getattr(settings, "smtp_from", "") or f"ark@{_socket.gethostname()}.{relay_domain}"

    # Build: multipart/mixed → multipart/alternative (plain + related(html + logo))
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(plain, "plain"))

    # HTML + inline logo as multipart/related
    html_related = MIMEMultipart("related")
    html_related.attach(MIMEText(html, "html"))
    if logo_path.exists():
        with open(logo_path, "rb") as f:
            logo = MIMEImage(f.read(), _subtype="png")
        logo.add_header("Content-ID", "<ark_logo>")
        logo.add_header("Content-Disposition", "inline", filename="logo.png")
        html_related.attach(logo)
    msg.attach(html_related)

    msg["From"] = f"ARK Research Portal <{from_addr}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=f"{_socket.gethostname()}.{relay_domain}")
    msg_str = msg.as_string()

    # Try KAUST relay first
    try:
        with smtplib.SMTP(relay_host, 25, timeout=10) as server:
            server.sendmail(from_addr, to_email, msg_str)
        logger.info(f"Welcome email sent via relay to {to_email}")
        return True
    except Exception as e:
        logger.warning(f"Relay failed ({e}), trying SMTP auth...")

    # Try SMTP with auth
    if settings.smtp_user and settings.smtp_password:
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(from_addr, to_email, msg_str)
            logger.info(f"Welcome email sent via SMTP to {to_email}")
            return True
        except Exception as e:
            logger.warning(f"SMTP failed ({e}), trying sendmail fallback...")

    return _sendmail_fallback(from_addr, to_email, msg_str)


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
