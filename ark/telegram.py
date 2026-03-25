"""Dedicated Telegram bot dispatcher for a single ARK project.

Each project gets its own bot token — no routing, no shared state, no daemon.

Config stored in ~/.ark/telegram.yaml:
    bot_token: "..."
    chat_id: "..."
"""

import json
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
import yaml
from pathlib import Path
from typing import Callable, Optional


# ══════════════════════════════════════════════════════════════
#  Config
# ══════════════════════════════════════════════════════════════

class TelegramConfig:
    """Loads Telegram credentials with backward-compatible fallback chain.

    Lookup order:
    1. project config dict   (per-project, wins)
    2. ~/.ark/telegram.yaml  (global fallback)
    3. environment variables  (ARK_TELEGRAM_BOT_TOKEN, ARK_TELEGRAM_CHAT_ID)
    """

    GLOBAL_CONFIG_PATH = Path.home() / ".ark" / "telegram.yaml"

    def __init__(self, project_config: dict = None):
        self._project_config = project_config or {}
        self._global = None

    def _load_global(self) -> dict:
        if self._global is None:
            if self.GLOBAL_CONFIG_PATH.exists():
                try:
                    with open(self.GLOBAL_CONFIG_PATH) as f:
                        self._global = yaml.safe_load(f) or {}
                except Exception:
                    self._global = {}
            else:
                self._global = {}
        return self._global

    @property
    def bot_token(self) -> Optional[str]:
        # 1. Project config (per-project, wins)
        token = self._project_config.get("telegram_bot_token")
        if token:
            return str(token)
        # 2. Global config
        token = self._load_global().get("bot_token")
        if token:
            return str(token)
        # 3. Env var
        return os.environ.get("ARK_TELEGRAM_BOT_TOKEN")

    @property
    def chat_id(self) -> Optional[str]:
        # 1. Project config (per-project, wins)
        cid = self._project_config.get("telegram_chat_id")
        if cid:
            return str(cid)
        # 2. Global config
        cid = self._load_global().get("chat_id")
        if cid:
            return str(cid)
        return os.environ.get("ARK_TELEGRAM_CHAT_ID", "")

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def save(self, bot_token: str, chat_id: str):
        """Write credentials to ~/.ark/telegram.yaml."""
        self.GLOBAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {"bot_token": bot_token, "chat_id": str(chat_id)}
        with open(self.GLOBAL_CONFIG_PATH, "w") as f:
            yaml.dump(data, f, default_flow_style=False)
        self._global = data

    @classmethod
    def from_project_config(cls, project_config: dict) -> 'TelegramConfig':
        """Create config with project fallback (backward compat)."""
        return cls(project_config=project_config)


# ══════════════════════════════════════════════════════════════
#  Dispatcher
# ══════════════════════════════════════════════════════════════

class TelegramDispatcher:
    """Dedicated Telegram bot for a single project.

    No routing, no mailbox, no daemon coordination — each project
    has its own bot token and polls getUpdates directly.
    """

    def __init__(self, project_name: str, config: TelegramConfig = None):
        self._project = project_name
        self._config = config or TelegramConfig()

        # Threading
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Message callback
        self._on_message: Optional[Callable[[str], None]] = None

        # Blocking ask() support
        self._ask_reply: Optional[str] = None
        self._ask_event = threading.Event()
        self._is_waiting = False

    @property
    def is_configured(self) -> bool:
        return self._config.is_configured

    # ── Lifecycle ──────────────────────────────────────────

    def start(self, on_message: Callable[[str], None] = None):
        """Start background polling."""
        if not self.is_configured:
            return

        self._on_message = on_message
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def stop(self):
        """Stop background polling."""
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None

    # ── Sending ────────────────────────────────────────────

    def send(self, text: str, parse_mode: str = ""):
        """Send a message (no prefix — dedicated bot)."""
        if not self.is_configured:
            return
        self._send_impl(text, parse_mode)

    def send_raw(self, text: str, parse_mode: str = ""):
        """Send without any decoration. Same as send() for dedicated bot."""
        if not self.is_configured:
            return
        self._send_impl(text, parse_mode)

    def _send_impl(self, text: str, parse_mode: str = ""):
        """Send text message, splitting if >4000 chars."""
        token = self._config.bot_token
        chat_id = self._config.chat_id
        if not token or not chat_id:
            return

        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            data = {"chat_id": chat_id, "text": chunk}
            if parse_mode:
                data["parse_mode"] = parse_mode
            self._api_call("sendMessage", **data)

    def send_document(self, file_path: Path, caption: str = "") -> bool:
        """Send a file (PDF, etc.) via Telegram.

        Returns True if the upload succeeded and Telegram confirmed the
        correct file size, False otherwise.
        """
        token = self._config.bot_token
        chat_id = self._config.chat_id
        if not token or not chat_id:
            return False

        file_path = Path(file_path)
        if not file_path.exists():
            return False

        local_size = file_path.stat().st_size
        if local_size < 1024:
            return False

        file_data = file_path.read_bytes()
        if not file_data[:5] == b'%PDF-':
            return False

        safe_caption = caption or ""
        if len(safe_caption) > 1024:
            safe_caption = safe_caption[:1021] + "..."

        boundary = uuid.uuid4().hex
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n',
            f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{safe_caption}\r\n',
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{file_path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n',
        ]
        body = "".join(parts).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

        url = f"https://api.telegram.org/bot{token}/sendDocument"
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            result = json.loads(resp.read().decode("utf-8"))
            if not result.get("ok"):
                raise RuntimeError(f"API error: {result.get('description', 'unknown')}")
            remote_size = result.get("result", {}).get("document", {}).get("file_size", 0)
            if remote_size and remote_size != local_size:
                self._send_impl(f"⚠️ PDF upload size mismatch: sent {local_size}B, Telegram got {remote_size}B")
                return False
            return True
        except Exception as e:
            self._send_impl(f"⚠️ Upload failed: {str(e)[:200]}")
            return False

    def send_typing(self):
        """Send typing indicator once."""
        token = self._config.bot_token
        chat_id = self._config.chat_id
        if not token or not chat_id:
            return
        self._api_call("sendChatAction", chat_id=chat_id, action="typing")

    # ── Receiving ──────────────────────────────────────────

    def ask(self, question: str, timeout: int = 1800) -> Optional[str]:
        """Send question, block until reply, return text or None on timeout."""
        if not self.is_configured:
            return None

        self.send(self.to_html(question), parse_mode="HTML")

        self._is_waiting = True
        self._ask_reply = None
        self._ask_event.clear()

        try:
            got_reply = self._ask_event.wait(timeout=timeout)
            if got_reply and self._ask_reply:
                self.send_raw("✅ Received, continuing...")
                return self._ask_reply
            return None
        finally:
            self._is_waiting = False

    # ── Polling ───────────────────────────────────────────

    def _poll_loop(self):
        """Background thread: poll getUpdates directly."""
        token = self._config.bot_token
        if not token:
            return

        # Get baseline offset
        offset = 0
        try:
            resp = self._api_call("getUpdates", limit=1, offset=-1)
            if resp and resp.get("result"):
                offset = resp["result"][-1]["update_id"] + 1
        except Exception:
            pass

        while not self._stop_event.is_set():
            try:
                resp = self._api_call("getUpdates", offset=offset, timeout=10, limit=10)
                if resp and resp.get("ok"):
                    for update in resp["result"]:
                        offset = update["update_id"] + 1
                        text = update.get("message", {}).get("text", "")
                        if text:
                            self._handle_incoming(text)
            except Exception:
                pass
            self._stop_event.wait(1)

    def _handle_incoming(self, text: str):
        """Handle an incoming message for this project."""
        if self._is_waiting:
            self._ask_reply = text
            self._ask_event.set()
            return

        if self._on_message:
            threading.Thread(target=self._on_message, args=(text,), daemon=True).start()

    # ── Telegram API ───────────────────────────────────────

    def _api_call(self, method: str, **params) -> Optional[dict]:
        """Make a Telegram Bot API call. Returns parsed JSON or None."""
        token = self._config.bot_token
        if not token:
            return None

        url = f"https://api.telegram.org/bot{token}/{method}"
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=max(params.get("timeout", 10) + 5, 15))
            return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    # ── Format Conversion ─────────────────────────────────

    @staticmethod
    def to_html(text: str) -> str:
        """Convert Markdown text to Telegram-compatible HTML.

        Telegram HTML supports: <b>, <i>, <code>, <pre>, <a href="">.
        This is more reliable than Telegram's Markdown modes which have
        quirky escaping rules.
        """
        if not text:
            return ""

        import html as _html

        code_blocks = []
        def _save_block(m):
            code_blocks.append(m.group(0))
            return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

        result = re.sub(r'```(\w*)\n?([\s\S]*?)```', _save_block, text)
        result = re.sub(r'`([^`]+)`', _save_block, result)

        result = _html.escape(result)

        # ## Headers → bold
        result = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', result, flags=re.MULTILINE)
        # **bold** → <b>
        result = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', result)
        # *italic* → <i>
        result = re.sub(r'(?<!\*)\*([^\*\n]+?)\*(?!\*)', r'<i>\1</i>', result)
        # [text](url) → <a href>
        result = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', result)

        for i, block in enumerate(code_blocks):
            original = block
            if original.startswith('```'):
                inner = re.sub(r'^```\w*\n?', '', original)
                inner = re.sub(r'```$', '', inner).strip()
                replacement = f'<pre>{_html.escape(inner)}</pre>'
            else:
                inner = original.strip('`')
                replacement = f'<code>{_html.escape(inner)}</code>'
            result = result.replace(f"\x00CODEBLOCK{i}\x00", replacement)

        if len(result) > 4000:
            result = result[:3997] + "..."

        return result

    @staticmethod
    def to_telegram_md(text: str) -> str:
        """Deprecated: use to_html() instead. Kept for backward compat."""
        return TelegramDispatcher.to_html(text)


# ══════════════════════════════════════════════════════════════
#  Convenience for CLI (non-orchestrator) usage
# ══════════════════════════════════════════════════════════════

def get_config(project_config: dict = None) -> TelegramConfig:
    """Get a TelegramConfig with optional project fallback."""
    return TelegramConfig(project_config=project_config)


def send_and_wait(project_config: dict, message: str,
                  project_name: str = "ark", timeout: int = 1800) -> Optional[str]:
    """Standalone send-and-wait for CLI flows (template download, research confirm).

    Creates a temporary dispatcher, sends message, waits for reply, cleans up.
    Returns reply text or None.
    """
    config = TelegramConfig(project_config=project_config)
    if not config.is_configured:
        return None

    dispatcher = TelegramDispatcher(project_name, config)
    dispatcher.start()
    try:
        reply = dispatcher.ask(message, timeout=timeout)
        return reply
    finally:
        dispatcher.stop()


def send_once(project_config: dict, message: str,
              project_name: str = "ark", parse_mode: str = ""):
    """Send a single message without starting a background poller."""
    config = TelegramConfig(project_config=project_config)
    if not config.is_configured:
        return

    token = config.bot_token
    chat_id = config.chat_id

    data = json.dumps({"chat_id": chat_id, "text": message, **({"parse_mode": parse_mode} if parse_mode else {})}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass
