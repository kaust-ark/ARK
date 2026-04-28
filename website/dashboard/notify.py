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

logger = logging.getLogger("website.dashboard.notify")

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
    """Send a Telegram message. Returns True on success.

    Uses a permissive TLS context to tolerate self-signed certs in the
    outbound TLS chain (seen on some KAUST-interior networks). Same
    relaxation that ark.telegram.TelegramBot.send_document applies.
    """
    import ssl
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
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            return r.status == 200
    except Exception as e:
        logger.warning(f"Telegram notify failed: {e}")
        return False


def send_telegram_login_link(link: str) -> bool:
    """Send a magic login link via Telegram. Returns True on success."""
    return send_telegram_notify(
        f"🔑 <b>ARK login link</b>\n<a href='{link}'>{link}</a>\nExpires in 1 hour."
    )


def send_telegram_document(file_path: str, caption: str = "",
                            bot_token: str = None, chat_id: str = None) -> bool:
    """Upload a file (typically PDF) to Telegram. Returns True on success.

    Mirrors the validator in ark.telegram.TelegramBot.send_document:
    file must exist, be at least 1KB, and (if caption is provided)
    caption is truncated to Telegram's 1024 char cap.
    """
    import ssl, uuid
    token = bot_token or None
    if not token or not chat_id:
        return False
    p = Path(file_path)
    if not p.exists() or p.stat().st_size < 1024:
        return False
    data = p.read_bytes()
    safe_caption = (caption or "")[:1020]
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n',
        f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{safe_caption}\r\n',
        f'--{boundary}\r\nContent-Disposition: form-data; name="parse_mode"\r\n\r\nHTML\r\n',
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{p.name}"\r\nContent-Type: application/octet-stream\r\n\r\n',
    ]
    body = "".join(parts).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            result = json.loads(r.read().decode("utf-8"))
        if not result.get("ok"):
            logger.warning(f"Telegram sendDocument failed: {result.get('description')}")
            return False
        return True
    except Exception as e:
        logger.warning(f"Telegram sendDocument error: {e}")
        return False


# ── Email ─────────────────────────────────────────────────────────────────────

def send_completion_email(
    settings,
    to_email: str,
    project_name: str,
    score: float,
    pdf_path: str | None,
    project_url: str,
    extra_pdf_paths: list[str] | None = None,
) -> bool:
    """Send a project completion email. Returns True on success.

    ``pdf_path`` is the primary attachment (typically paper/main.pdf).
    ``extra_pdf_paths`` holds additional PDFs like the summary report;
    each is attached after the primary one in list order.
    """
    from email.utils import formatdate, make_msgid
    from email.mime.image import MIMEImage

    if not (settings.smtp_user and settings.smtp_password):
        logger.warning("SMTP credentials not configured — skipping email.")
        return False

    logo_path = Path(__file__).parent / "static" / "logo_ark_transparent.png"

    subject = f"[ARK] '{project_name}' finished — {score:.1f}/10"

    has_paper = bool(pdf_path and Path(pdf_path).exists())
    has_summary = bool(
        extra_pdf_paths
        and any(p and Path(p).exists() for p in extra_pdf_paths)
    )

    # Rating badge, mirrors reviewer's score bands roughly.
    if score >= 8:
        rating_label, rating_color = "Accept", "#0d9488"
    elif score >= 6.5:
        rating_label, rating_color = "Weak Accept", "#0d9488"
    elif score >= 5:
        rating_label, rating_color = "Borderline", "#b08800"
    else:
        rating_label, rating_color = "Needs Work", "#b03a3a"

    # ── Plain-text fallback ──────────────────────────────────────────
    plain_lines = [
        f"Your ARK project '{project_name}' finished.",
        "",
        f"Score: {score:.1f} / 10  ({rating_label})",
        f"Dashboard: {project_url}",
        "",
    ]
    if has_paper or has_summary:
        plain_lines.append("Attached:")
        if has_paper:
            plain_lines.append("  - Paper PDF (the submission-ready manuscript)")
        if has_summary:
            plain_lines.append("  - Run summary PDF (what landed, what didn't, next steps)")
        plain_lines.append("")
    plain_lines.append("-- ARK Team")
    plain = "\n".join(plain_lines)

    # ── HTML ─────────────────────────────────────────────────────────
    attachment_cards = ""
    if has_paper:
        attachment_cards += """\
<tr>
  <td style="padding:12px 16px;background:#f0fdfa;border-radius:10px;border-left:4px solid #0d9488;">
    <p style="margin:0;color:#134e4a;font-size:14px;line-height:1.6;">
      <strong style="color:#0d9488;">Paper PDF</strong><br/>
      Compiled manuscript, ready for reviewer eyes.
    </p>
  </td>
</tr>
<tr><td style="height:10px;"></td></tr>
"""
    if has_summary:
        attachment_cards += """\
<tr>
  <td style="padding:12px 16px;background:#f0fdfa;border-radius:10px;border-left:4px solid #0d9488;">
    <p style="margin:0;color:#134e4a;font-size:14px;line-height:1.6;">
      <strong style="color:#0d9488;">Run Summary PDF</strong><br/>
      What the idea asked for, what landed, what didn&rsquo;t, unresolved reviewer concerns, and recommended next steps.
    </p>
  </td>
</tr>
<tr><td style="height:10px;"></td></tr>
"""

    from html import escape as _esc
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
    <h1 style="margin:0;color:#ffffff;font-size:26px;font-weight:700;letter-spacing:-0.3px;">
      Project Finished
    </h1>
    <p style="margin:10px 0 0;color:#ccfbf1;font-size:14px;">
      {_esc(project_name)}
    </p>
  </td></tr>

  <!-- Score strip -->
  <tr><td style="padding:28px 44px 10px;text-align:center;">
    <div style="display:inline-block;padding:6px 14px;border-radius:999px;background:{rating_color};color:#fff;font-size:12px;font-weight:700;letter-spacing:0.5px;text-transform:uppercase;">{rating_label}</div>
    <p style="margin:14px 0 0;color:#111;font-size:42px;font-weight:700;line-height:1;">{score:.1f}<span style="color:#888;font-size:18px;font-weight:500;"> / 10</span></p>
  </td></tr>

  <!-- Body -->
  <tr><td style="padding:20px 44px 10px;">
    <p style="margin:0 0 20px;color:#333;font-size:15px;line-height:1.7;">
      Your ARK project just finished its review loop. The manuscript and a
      run-summary report are attached below &mdash; the summary walks through
      what the idea asked for, what landed in the paper, what was deferred
      or blocked, and a short list of recommended next steps.
    </p>

    <!-- Attachment cards -->
    <table width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0 10px;">
      {attachment_cards}
    </table>

    <!-- CTA Button -->
    <table width="100%" cellpadding="0" cellspacing="0" style="margin:22px 0 6px;">
      <tr><td align="center">
        <a href="{project_url}" style="display:inline-block;background:#0d9488;color:#fff;
           font-size:15px;font-weight:600;padding:14px 36px;border-radius:8px;
           text-decoration:none;letter-spacing:0.3px;">
          Open Dashboard
        </a>
      </td></tr>
    </table>

    <p style="margin:20px 0 0;color:#555;font-size:13px;line-height:1.7;">
      Reply to this email or reach out at
      <a href="mailto:contact@idea2paper.org" style="color:#0d9488;text-decoration:none;font-weight:600;">contact@idea2paper.org</a>
      if something in the summary looks off.
    </p>
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f8fffe;padding:22px 44px;border-top:1px solid #e0f2f1;">
    <p style="margin:0;color:#999;font-size:12px;line-height:1.5;text-align:center;">
      ARK &mdash; Automatic Research Kit<br/>
      You received this email because a project you submitted just finished.
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>
"""

    from_addr = getattr(settings, "smtp_from", "") or "contact@idea2paper.org"

    # multipart/mixed (for attachments) → multipart/alternative (plain + related(html + logo))
    msg = MIMEMultipart("mixed")
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain, "plain"))

    html_related = MIMEMultipart("related")
    html_related.attach(MIMEText(html, "html"))
    if logo_path.exists():
        with open(logo_path, "rb") as f:
            logo = MIMEImage(f.read(), _subtype="png")
        logo.add_header("Content-ID", "<ark_logo>")
        logo.add_header("Content-Disposition", "inline", filename="logo.png")
        html_related.attach(logo)
    alt.attach(html_related)
    msg.attach(alt)

    msg["From"] = f"ARK Team <{from_addr}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="idea2paper.org")

    def _attach(path_str: str | None):
        if not path_str:
            return
        p = Path(path_str)
        if not p.exists():
            return
        with open(p, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{p.name}"',
        )
        msg.attach(part)

    _attach(pdf_path)
    for extra in (extra_pdf_paths or []):
        _attach(extra)

    # Try relay if explicitly configured
    relay = getattr(settings, "smtp_relay", "")
    if relay:
        try:
            with smtplib.SMTP(relay, 25, timeout=10) as server:
                server.sendmail(from_addr, to_email, msg.as_string())
            logger.info(f"Completion email sent via relay to {to_email} for project '{project_name}'")
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

We are actively iterating and would love your feedback. Just reply to
this email or reach out at contact@idea2paper.org.

-- ARK Team
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
      We&rsquo;re actively iterating on the platform and would love your feedback.
    </p>
    <p style="margin:0 0 8px;color:#333;font-size:15px;line-height:1.7;">
      Found a bug? Have an idea? Just reply to this email or reach out at
      <a href="mailto:contact@idea2paper.org" style="color:#0d9488;text-decoration:none;font-weight:600;">
        contact@idea2paper.org</a>.
    </p>
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f8fffe;padding:24px 44px;border-top:1px solid #e0f2f1;">
    <p style="margin:0;color:#999;font-size:12px;line-height:1.5;text-align:center;">
      ARK &mdash; Automatic Research Kit<br/>
      You received this email because you signed up for ARK.
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>
"""

    from_addr = getattr(settings, "smtp_from", "") or "contact@idea2paper.org"

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

    msg["From"] = f"ARK Team <{from_addr}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="idea2paper.org")
    msg_str = msg.as_string()

    # Try relay if explicitly configured
    relay = getattr(settings, "smtp_relay", "")
    if relay:
        try:
            with smtplib.SMTP(relay, 25, timeout=10) as server:
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
    from_addr = getattr(settings, "smtp_from", "") or "contact@idea2paper.org"
    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))
    msg_str = msg.as_string()

    # Try relay if explicitly configured
    relay = getattr(settings, "smtp_relay", "")
    if relay:
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


def send_access_granted_email(settings, to_email: str, dashboard_url: str) -> bool:
    """Notify a user that they've been added to the ARK Dashboard allowlist.

    The user still has to verify via Cloudflare Access (one-time code sent
    when they hit the dashboard), so the email is informational + CTA.
    """
    from email.utils import formatdate, make_msgid
    from email.mime.image import MIMEImage

    logo_path = Path(__file__).parent / "static" / "logo_ark_transparent.png"

    subject = "You're in — ARK Dashboard access granted"

    plain = f"""\
You've been granted access to the ARK Dashboard.

Sign in: {dashboard_url}

When you visit, Cloudflare Access will email a one-time code to this
address to verify your identity. Use the same email ({to_email}) to
sign in.

Note: authorization may take up to 24 hours to fully propagate. If
you hit a "not authorized" page in your normal browser during that
window, try an incognito / private window instead.

If you weren't expecting this, you can ignore this email.

-- ARK Team
"""

    from html import escape as _esc
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
    <h1 style="margin:0;color:#ffffff;font-size:26px;font-weight:700;letter-spacing:-0.3px;">
      Access Granted
    </h1>
    <p style="margin:10px 0 0;color:#ccfbf1;font-size:14px;">
      ARK Dashboard &mdash; Automatic Research Kit
    </p>
  </td></tr>

  <!-- Body -->
  <tr><td style="padding:32px 44px 10px;">
    <p style="margin:0 0 18px;color:#333;font-size:15px;line-height:1.7;">
      Good news &mdash; <strong>{_esc(to_email)}</strong> has been added to the
      ARK Dashboard allowlist. You can now sign in and start running projects.
    </p>

    <table width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0 6px;">
      <tr>
        <td style="padding:14px 16px;background:#f0fdfa;border-radius:10px;border-left:4px solid #0d9488;">
          <p style="margin:0;color:#134e4a;font-size:14px;line-height:1.6;">
            <strong style="color:#0d9488;">How sign-in works</strong><br/>
            Visit the dashboard, enter this email, and Cloudflare Access will
            email you a one-time code to verify your identity.
          </p>
        </td>
      </tr>
      <tr><td style="height:10px;"></td></tr>
      <tr>
        <td style="padding:14px 16px;background:#fffbeb;border-radius:10px;border-left:4px solid #b08800;">
          <p style="margin:0;color:#5b4500;font-size:14px;line-height:1.6;">
            <strong style="color:#b08800;">Heads up &mdash; up to 24h to propagate</strong><br/>
            Authorization may take up to 24 hours to fully take effect. If
            you hit a &ldquo;not authorized&rdquo; page in your normal browser
            during that window, try an <strong>incognito / private window</strong>
            instead.
          </p>
        </td>
      </tr>
    </table>

    <!-- CTA Button -->
    <table width="100%" cellpadding="0" cellspacing="0" style="margin:26px 0 8px;">
      <tr><td align="center">
        <a href="{dashboard_url}" style="display:inline-block;background:#0d9488;color:#fff;
           font-size:15px;font-weight:600;padding:14px 36px;border-radius:8px;
           text-decoration:none;letter-spacing:0.3px;">
          Open ARK Dashboard
        </a>
      </td></tr>
    </table>

    <p style="margin:24px 0 0;color:#555;font-size:13px;line-height:1.7;">
      Questions or need a hand getting started? Reply here or write to
      <a href="mailto:contact@idea2paper.org" style="color:#0d9488;text-decoration:none;font-weight:600;">contact@idea2paper.org</a>.
    </p>
    <p style="margin:14px 0 0;color:#888;font-size:12px;line-height:1.6;">
      If you weren&rsquo;t expecting this email, you can safely ignore it.
    </p>
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f8fffe;padding:22px 44px;border-top:1px solid #e0f2f1;">
    <p style="margin:0;color:#999;font-size:12px;line-height:1.5;text-align:center;">
      ARK &mdash; Automatic Research Kit<br/>
      Sent because your email was added to the ARK Dashboard allowlist.
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>
"""

    from_addr = getattr(settings, "smtp_from", "") or "contact@idea2paper.org"

    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(plain, "plain"))

    html_related = MIMEMultipart("related")
    html_related.attach(MIMEText(html, "html"))
    if logo_path.exists():
        with open(logo_path, "rb") as f:
            logo = MIMEImage(f.read(), _subtype="png")
        logo.add_header("Content-ID", "<ark_logo>")
        logo.add_header("Content-Disposition", "inline", filename="logo.png")
        html_related.attach(logo)
    msg.attach(html_related)

    msg["From"] = f"ARK Team <{from_addr}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="idea2paper.org")
    msg_str = msg.as_string()

    relay = getattr(settings, "smtp_relay", "")
    if relay:
        try:
            with smtplib.SMTP(relay, 25, timeout=10) as server:
                server.sendmail(from_addr, to_email, msg_str)
            logger.info(f"Access-granted email sent via relay to {to_email}")
            return True
        except Exception as e:
            logger.warning(f"Relay failed ({e}), trying SMTP auth…")

    if settings.smtp_user and settings.smtp_password:
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(from_addr, to_email, msg_str)
            logger.info(f"Access-granted email sent via SMTP to {to_email}")
            return True
        except Exception as e:
            logger.warning(f"SMTP failed ({e}), trying sendmail fallback…")

    return _sendmail_fallback(from_addr, to_email, msg_str)
