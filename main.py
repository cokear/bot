"""
TG 多功能机器人入口 - Python 版（单文件）
"""

import asyncio
import base64
import importlib
import importlib.util
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
import socket
import ctypes
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")
LOCAL_SITE_DIR = os.path.join(DATA_DIR, "site-packages")
if LOCAL_SITE_DIR not in sys.path:
    sys.path.insert(0, LOCAL_SITE_DIR)


def ensure_python_dependencies():
    required_modules = {
        "aiohttp": "aiohttp",
        "deep_translator": "deep-translator",
        "httpx": "httpx",
        "qrcode": "qrcode[pil]",
        "PIL": "qrcode[pil]",
        "telegram": "python-telegram-bot",
    }
    missing = [name for name in required_modules if importlib.util.find_spec(name) is None]
    if not missing:
        return
    requirements_path = os.path.join(ROOT_DIR, "requirements.txt")
    if not os.path.exists(requirements_path):
        raise RuntimeError("requirements.txt missing")
    os.makedirs(LOCAL_SITE_DIR, exist_ok=True)
    package_names = ", ".join(sorted({required_modules[name] for name in missing}))
    print(f"Installing missing Python dependencies: {package_names}", flush=True)
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--target",
        LOCAL_SITE_DIR,
        "-r",
        requirements_path,
    ]
    try:
        subprocess.check_call(command)
    except subprocess.CalledProcessError:
        subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
        subprocess.check_call(command)
    importlib.invalidate_caches()
    still_missing = [name for name in required_modules if importlib.util.find_spec(name) is None]
    if still_missing:
        raise RuntimeError("Dependency install failed: " + ", ".join(still_missing))


ensure_python_dependencies()

from aiohttp import web, ClientSession, ClientTimeout
from deep_translator import GoogleTranslator
import httpx
import qrcode
from qrcode.constants import ERROR_CORRECT_L
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
from zoneinfo import ZoneInfo


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.WARNING
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")
PUBLIC_DIR = os.path.join(ROOT_DIR, "public")
CONFIG_ENC_PATH = os.path.join(DATA_DIR, "config.enc")

ADMIN_PATH = "/admin"
BINARY_PATH = "/app"


def ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)


def xor_bytes(data: bytes, key: bytes) -> bytes:
    output = bytearray(len(data))
    for i, b in enumerate(data):
        output[i] = b ^ key[i % len(key)]
    return bytes(output)


def read_key_from_tools() -> str:
    file_path = os.path.join(PUBLIC_DIR, "tools.js")
    if not os.path.exists(file_path):
        raise RuntimeError("Key source missing")
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    matches = re.findall(r"__k(\d+):([A-Za-z0-9]+)", content)
    if len(matches) < 3:
        raise RuntimeError("Key parts missing")
    parts = sorted(((int(idx), val) for idx, val in matches), key=lambda x: x[0])
    return "".join(val for _, val in parts)


def encrypt_text(plain_text: str, key_text: str) -> str:
    key = key_text.encode("utf-8")
    if not key:
        raise RuntimeError("Empty key")
    data = str(plain_text).encode("utf-8")
    xored = xor_bytes(data, key)
    return base64.b64encode(xored).decode("utf-8")


def decrypt_text(base64_text: str, key_text: str) -> str:
    key = key_text.encode("utf-8")
    if not key:
        raise RuntimeError("Empty key")
    raw = base64.b64decode(str(base64_text).strip())
    xored = xor_bytes(raw, key)
    return xored.decode("utf-8")


def _load_user_config():
    try:
        sys.path.insert(0, ROOT_DIR)
        import config as user_config

        return user_config
    except Exception:
        return None


def build_default_config() -> dict:
    user_config = _load_user_config()
    default = {
        "BOT_TOKEN": "",
        "ADMIN_ID": "",
        "ADMIN_PASSWORD": "admin123",
        "TG_API_BASE": "",
        "BINARY_URL": "",
        "BINARY_PORT": None,
        "BINARY_AUTOSTART": False,
        "MAIL": {
            "HOST": "imap.gmail.com",
            "PORT": 993,
            "USER": "",
            "PASS": "",
            "DIGEST_TIME": "08:00",
        },
        "DB_PATH": "./data/bot.db",
        "OPENAI": {
            "API_BASE": "https://api.openai.com/v1",
            "API_KEY": "",
            "MODEL": "gpt-3.5-turbo",
        },
        "RSS": {
            "CHECK_INTERVAL": 30,
            "KEYWORDS": [],
            "EXCLUDE": [],
        },
        "FEATURES": {
            "TRANSLATE": True,
            "QRCODE": True,
            "SHORTEN": True,
            "REMIND": True,
            "NOTE": True,
            "RSS": True,
            "WEATHER": True,
            "RATE": True,
            "MAIL": False,
            "CHAT": True,
            "SKIP_TOKEN_CHECK": True,
        },
    }

    if user_config:
        default["BOT_TOKEN"] = getattr(user_config, "BOT_TOKEN", default["BOT_TOKEN"])
        default["ADMIN_ID"] = getattr(user_config, "ADMIN_ID", default["ADMIN_ID"])
        default["TG_API_BASE"] = getattr(
            user_config, "TG_API_BASE", default["TG_API_BASE"]
        )
        default["DB_PATH"] = getattr(user_config, "DB_PATH", default["DB_PATH"])
        openai_cfg = getattr(user_config, "OPENAI", {})
        if isinstance(openai_cfg, dict):
            default["OPENAI"].update(
                {
                    "API_BASE": openai_cfg.get(
                        "API_BASE", default["OPENAI"]["API_BASE"]
                    ),
                    "API_KEY": openai_cfg.get("API_KEY", default["OPENAI"]["API_KEY"]),
                    "MODEL": openai_cfg.get("MODEL", default["OPENAI"]["MODEL"]),
                }
            )
        rss_cfg = getattr(user_config, "RSS", {})
        if isinstance(rss_cfg, dict):
            default["RSS"].update(
                {
                    "CHECK_INTERVAL": rss_cfg.get(
                        "CHECK_INTERVAL", default["RSS"]["CHECK_INTERVAL"]
                    ),
                    "KEYWORDS": rss_cfg.get("KEYWORDS", default["RSS"]["KEYWORDS"]),
                    "EXCLUDE": rss_cfg.get("EXCLUDE", default["RSS"]["EXCLUDE"]),
                }
            )
        features_cfg = getattr(user_config, "FEATURES", {})
        if isinstance(features_cfg, dict):
            for key in default["FEATURES"]:
                if key in features_cfg:
                    default["FEATURES"][key] = bool(features_cfg.get(key))

    return default


def read_encrypted_config() -> dict:
    ensure_data_dir()
    if not os.path.exists(CONFIG_ENC_PATH):
        return build_default_config()
    key = read_key_from_tools()
    with open(CONFIG_ENC_PATH, "r", encoding="utf-8") as f:
        encrypted = f.read()
    json_text = decrypt_text(encrypted, key)
    return json.loads(json_text)


def write_encrypted_config(settings: dict):
    ensure_data_dir()
    key = read_key_from_tools()
    json_text = json.dumps(settings, ensure_ascii=True, indent=2)
    encrypted = encrypt_text(json_text, key)
    with open(CONFIG_ENC_PATH, "w", encoding="utf-8") as f:
        f.write(encrypted)


def build_runtime_config(settings: dict) -> dict:
    return {
        "bot_token": settings.get("BOT_TOKEN", ""),
        "admin_id": settings.get("ADMIN_ID", ""),
        "api_base": settings.get("TG_API_BASE", ""),
        "db_path": settings.get("DB_PATH", "./data/bot.db"),
        "binary": {
            "url": settings.get("BINARY_URL", ""),
            "port": settings.get("BINARY_PORT"),
            "autostart": bool(settings.get("BINARY_AUTOSTART", False)),
        },
        "openai": {
            "api_base": settings.get("OPENAI", {}).get(
                "API_BASE", "https://api.openai.com/v1"
            ),
            "api_key": settings.get("OPENAI", {}).get("API_KEY", ""),
            "model": settings.get("OPENAI", {}).get("MODEL", "gpt-3.5-turbo"),
        },
        "rss": {
            "check_interval": settings.get("RSS", {}).get("CHECK_INTERVAL", 30),
            "keywords": settings.get("RSS", {}).get("KEYWORDS", []),
            "exclude": settings.get("RSS", {}).get("EXCLUDE", []),
        },
        "features": {
            "translate": bool(settings.get("FEATURES", {}).get("TRANSLATE", False)),
            "qrcode": bool(settings.get("FEATURES", {}).get("QRCODE", False)),
            "shorten": bool(settings.get("FEATURES", {}).get("SHORTEN", False)),
            "remind": bool(settings.get("FEATURES", {}).get("REMIND", False)),
            "note": bool(settings.get("FEATURES", {}).get("NOTE", False)),
            "rss": bool(settings.get("FEATURES", {}).get("RSS", False)),
            "weather": bool(settings.get("FEATURES", {}).get("WEATHER", False)),
            "rate": bool(settings.get("FEATURES", {}).get("RATE", False)),
            "mail": bool(settings.get("FEATURES", {}).get("MAIL", False)),
            "chat": bool(settings.get("FEATURES", {}).get("CHAT", False)),
            "skip_token_check": bool(
                settings.get("FEATURES", {}).get("SKIP_TOKEN_CHECK", False)
            ),
        },
    }


def get_runtime_config() -> dict:
    return build_runtime_config(read_encrypted_config())


def validate_config():
    settings = read_encrypted_config()
    if settings.get("FEATURES", {}).get("SKIP_TOKEN_CHECK"):
        return True
    token = settings.get("BOT_TOKEN") or ""
    if not token or token == "your_bot_token_here":
        raise RuntimeError("Please configure BOT_TOKEN")
    return True


FEATURE_KEYS = [
    "TRANSLATE",
    "QRCODE",
    "SHORTEN",
    "REMIND",
    "NOTE",
    "RSS",
    "WEATHER",
    "RATE",
    "MAIL",
    "CHAT",
    "SKIP_TOKEN_CHECK",
]


def _to_number(value, fallback):
    if value == "" or value is None:
        return fallback
    try:
        num = int(value)
        return num
    except Exception:
        return fallback


def _normalize_binary_port(value, fallback):
    if value == "":
        return None
    if value is None:
        return fallback
    try:
        num = int(value)
        return num
    except Exception:
        return fallback


def normalize_settings(input_settings: dict, current_settings: dict) -> dict:
    next_settings = {
        "BOT_TOKEN": str(
            input_settings.get("BOT_TOKEN", current_settings.get("BOT_TOKEN", ""))
        ),
        "ADMIN_ID": str(
            input_settings.get("ADMIN_ID", current_settings.get("ADMIN_ID", ""))
        ),
        "TG_API_BASE": str(
            input_settings.get("TG_API_BASE", current_settings.get("TG_API_BASE", ""))
        ),
        "BINARY_URL": str(
            input_settings.get("BINARY_URL", current_settings.get("BINARY_URL", ""))
        ),
        "BINARY_PORT": _normalize_binary_port(
            input_settings.get("BINARY_PORT"), current_settings.get("BINARY_PORT")
        ),
        "BINARY_AUTOSTART": bool(
            input_settings.get(
                "BINARY_AUTOSTART", current_settings.get("BINARY_AUTOSTART", False)
            )
        ),
        "ADMIN_PASSWORD": current_settings.get("ADMIN_PASSWORD", ""),
        "MAIL": {
            "HOST": str(
                input_settings.get("MAIL", {}).get(
                    "HOST",
                    current_settings.get("MAIL", {}).get("HOST", "imap.gmail.com"),
                )
            ),
            "PORT": _to_number(
                input_settings.get("MAIL", {}).get("PORT"),
                current_settings.get("MAIL", {}).get("PORT", 993),
            ),
            "USER": str(
                input_settings.get("MAIL", {}).get(
                    "USER", current_settings.get("MAIL", {}).get("USER", "")
                )
            ),
            "PASS": str(
                input_settings.get("MAIL", {}).get(
                    "PASS", current_settings.get("MAIL", {}).get("PASS", "")
                )
            ),
            "DIGEST_TIME": str(
                input_settings.get("MAIL", {}).get(
                    "DIGEST_TIME",
                    current_settings.get("MAIL", {}).get("DIGEST_TIME", "08:00"),
                )
            ),
        },
        "DB_PATH": str(
            input_settings.get(
                "DB_PATH", current_settings.get("DB_PATH", "./data/bot.db")
            )
        ),
        "OPENAI": {
            "API_BASE": str(
                input_settings.get("OPENAI", {}).get(
                    "API_BASE",
                    current_settings.get("OPENAI", {}).get(
                        "API_BASE", "https://api.openai.com/v1"
                    ),
                )
            ),
            "API_KEY": str(
                input_settings.get("OPENAI", {}).get(
                    "API_KEY", current_settings.get("OPENAI", {}).get("API_KEY", "")
                )
            ),
            "MODEL": str(
                input_settings.get("OPENAI", {}).get(
                    "MODEL",
                    current_settings.get("OPENAI", {}).get("MODEL", "gpt-3.5-turbo"),
                )
            ),
        },
        "RSS": {
            "CHECK_INTERVAL": _to_number(
                input_settings.get("RSS", {}).get("CHECK_INTERVAL"),
                current_settings.get("RSS", {}).get("CHECK_INTERVAL", 30),
            ),
            "KEYWORDS": input_settings.get("RSS", {}).get("KEYWORDS")
            if isinstance(input_settings.get("RSS", {}).get("KEYWORDS"), list)
            else current_settings.get("RSS", {}).get("KEYWORDS", []),
            "EXCLUDE": input_settings.get("RSS", {}).get("EXCLUDE")
            if isinstance(input_settings.get("RSS", {}).get("EXCLUDE"), list)
            else current_settings.get("RSS", {}).get("EXCLUDE", []),
        },
        "FEATURES": {},
    }

    incoming_password = input_settings.get("ADMIN_PASSWORD")
    if isinstance(incoming_password, str) and incoming_password.strip():
        next_settings["ADMIN_PASSWORD"] = incoming_password.strip()

    for key in FEATURE_KEYS:
        incoming = input_settings.get("FEATURES", {}).get(key)
        if isinstance(incoming, bool):
            next_settings["FEATURES"][key] = incoming
        elif isinstance(current_settings.get("FEATURES", {}).get(key), bool):
            next_settings["FEATURES"][key] = current_settings["FEATURES"][key]
        else:
            next_settings["FEATURES"][key] = False

    return next_settings


def filter_response(settings: dict) -> dict:
    return {
        "BOT_TOKEN": settings.get("BOT_TOKEN", ""),
        "ADMIN_ID": settings.get("ADMIN_ID", ""),
        "TG_API_BASE": settings.get("TG_API_BASE", ""),
        "BINARY_URL": settings.get("BINARY_URL", ""),
        "BINARY_PORT": settings.get("BINARY_PORT", "")
        if settings.get("BINARY_PORT") is not None
        else "",
        "BINARY_AUTOSTART": bool(settings.get("BINARY_AUTOSTART", False)),
        "ADMIN_PASSWORD": "",
        "MAIL": {
            "HOST": settings.get("MAIL", {}).get("HOST", "imap.gmail.com"),
            "PORT": settings.get("MAIL", {}).get("PORT", 993),
            "USER": settings.get("MAIL", {}).get("USER", ""),
            "PASS": settings.get("MAIL", {}).get("PASS", ""),
            "DIGEST_TIME": settings.get("MAIL", {}).get("DIGEST_TIME", "08:00"),
        },
        "DB_PATH": settings.get("DB_PATH", "./data/bot.db"),
        "OPENAI": {
            "API_BASE": settings.get("OPENAI", {}).get(
                "API_BASE", "https://api.openai.com/v1"
            ),
            "API_KEY": settings.get("OPENAI", {}).get("API_KEY", ""),
            "MODEL": settings.get("OPENAI", {}).get("MODEL", "gpt-3.5-turbo"),
        },
        "RSS": {
            "CHECK_INTERVAL": settings.get("RSS", {}).get("CHECK_INTERVAL", 30),
            "KEYWORDS": settings.get("RSS", {}).get("KEYWORDS", []),
            "EXCLUDE": settings.get("RSS", {}).get("EXCLUDE", []),
        },
        "FEATURES": {
            key: bool(settings.get("FEATURES", {}).get(key)) for key in FEATURE_KEYS
        },
    }


def get_web_port() -> int:
    return int(
        os.environ.get("SERVER_PORT")
        or os.environ.get("PORT")
        or os.environ.get("PRIMARY_PORT")
        or os.environ.get("P_SERVER_PORT")
        or 3097
    )


class ConfigProxy:
    def __getattr__(self, name):
        runtime = get_runtime_config()
        if name in runtime:
            return runtime[name]
        raise AttributeError(name)

    def validate(self):
        return validate_config()

    @property
    def web_port(self):
        return get_web_port()


config = ConfigProxy()


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_dir()
        import sqlite3

        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def _ensure_dir(self):
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        if not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

    def execute(self, sql: str, params: tuple = ()):  # noqa: D401
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()


db = Database(config.db_path)


def init_database():
    db.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            message TEXT NOT NULL,
            remind_at INTEGER NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s', 'now')),
            sent INTEGER DEFAULT 0
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s', 'now'))
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS rss_feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            last_item_id TEXT,
            created_at INTEGER DEFAULT (strftime('%s', 'now'))
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS mail_config (
            user_id TEXT PRIMARY KEY,
            host TEXT NOT NULL,
            port INTEGER DEFAULT 993,
            email TEXT NOT NULL,
            password TEXT NOT NULL,
            digest_time TEXT DEFAULT '08:00',
            enabled INTEGER DEFAULT 1
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS user_timezone (
            user_id TEXT PRIMARY KEY,
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS rss_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'include'
        )
    """)

    db.commit()


class ReminderDB:
    @staticmethod
    def add(user_id: str, chat_id: str, message: str, remind_at: int) -> int:
        cursor = db.execute(
            "INSERT INTO reminders (user_id, chat_id, message, remind_at) VALUES (?, ?, ?, ?)",
            (user_id, chat_id, message, remind_at),
        )
        db.commit()
        return int(cursor.lastrowid or 0)

    @staticmethod
    def get_pending():
        now = int(time.time())
        cursor = db.execute(
            "SELECT * FROM reminders WHERE remind_at <= ? AND sent = 0", (now,)
        )
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def mark_sent(reminder_id: int):
        db.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
        db.commit()

    @staticmethod
    def list_by_user(user_id: str):
        cursor = db.execute(
            "SELECT * FROM reminders WHERE user_id = ? AND sent = 0 ORDER BY remind_at",
            (user_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def delete(reminder_id: int, user_id: str) -> int:
        cursor = db.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id)
        )
        db.commit()
        return int(cursor.rowcount or 0)


class NoteDB:
    @staticmethod
    def add(user_id: str, content: str) -> int:
        cursor = db.execute(
            "INSERT INTO notes (user_id, content) VALUES (?, ?)", (user_id, content)
        )
        db.commit()
        return int(cursor.lastrowid or 0)

    @staticmethod
    def list(user_id: str, limit: int = 10):
        cursor = db.execute(
            "SELECT * FROM notes WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def delete(note_id: int, user_id: str) -> int:
        cursor = db.execute(
            "DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)
        )
        db.commit()
        return int(cursor.rowcount or 0)

    @staticmethod
    def clear(user_id: str) -> int:
        cursor = db.execute("DELETE FROM notes WHERE user_id = ?", (user_id,))
        db.commit()
        return int(cursor.rowcount or 0)


class RssDB:
    @staticmethod
    def add(user_id: str, chat_id: str, url: str, title: str) -> int:
        cursor = db.execute(
            "INSERT INTO rss_feeds (user_id, chat_id, url, title) VALUES (?, ?, ?, ?)",
            (user_id, chat_id, url, title),
        )
        db.commit()
        return int(cursor.lastrowid or 0)

    @staticmethod
    def list(user_id: str):
        cursor = db.execute("SELECT * FROM rss_feeds WHERE user_id = ?", (user_id,))
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def get_all():
        cursor = db.execute("SELECT * FROM rss_feeds")
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def update_last_item(feed_id: int, last_item_id: str):
        db.execute(
            "UPDATE rss_feeds SET last_item_id = ? WHERE id = ?",
            (last_item_id, feed_id),
        )
        db.commit()

    @staticmethod
    def delete(feed_id: int, user_id: str) -> int:
        cursor = db.execute(
            "DELETE FROM rss_feeds WHERE id = ? AND user_id = ?", (feed_id, user_id)
        )
        db.commit()
        return int(cursor.rowcount or 0)


class SettingsDB:
    @staticmethod
    def get(key: str, default: Optional[str] = None):
        cursor = db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default

    @staticmethod
    def set(key: str, value: str):
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        db.commit()


class TimezoneDB:
    @staticmethod
    def get(user_id: str) -> str:
        cursor = db.execute(
            "SELECT timezone FROM user_timezone WHERE user_id = ?", (user_id,)
        )
        row = cursor.fetchone()
        return row["timezone"] if row else "Asia/Shanghai"

    @staticmethod
    def set(user_id: str, timezone: str):
        db.execute(
            "INSERT OR REPLACE INTO user_timezone (user_id, timezone) VALUES (?, ?)",
            (user_id, timezone),
        )
        db.commit()


class KeywordDB:
    @staticmethod
    def add(keyword: str, keyword_type: str = "include") -> int:
        cursor = db.execute(
            "SELECT id FROM rss_keywords WHERE keyword = ? AND type = ?",
            (keyword, keyword_type),
        )
        if cursor.fetchone():
            return 0
        cursor = db.execute(
            "INSERT INTO rss_keywords (keyword, type) VALUES (?, ?)",
            (keyword, keyword_type),
        )
        db.commit()
        return int(cursor.rowcount or 0)

    @staticmethod
    def list(keyword_type: Optional[str] = None):
        if keyword_type:
            cursor = db.execute(
                "SELECT * FROM rss_keywords WHERE type = ?", (keyword_type,)
            )
        else:
            cursor = db.execute("SELECT * FROM rss_keywords")
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def delete(keyword: str, keyword_type: str) -> int:
        cursor = db.execute(
            "DELETE FROM rss_keywords WHERE keyword = ? AND type = ?",
            (keyword, keyword_type),
        )
        db.commit()
        return int(cursor.rowcount or 0)

    @staticmethod
    def get_keywords():
        cursor = db.execute("SELECT keyword FROM rss_keywords WHERE type = 'include'")
        return [row["keyword"] for row in cursor.fetchall()]

    @staticmethod
    def get_excludes():
        cursor = db.execute("SELECT keyword FROM rss_keywords WHERE type = 'exclude'")
        return [row["keyword"] for row in cursor.fetchall()]


reminder_db = ReminderDB()
note_db = NoteDB()
rss_db = RssDB()
settings_db = SettingsDB()
timezone_db = TimezoneDB()
keyword_db = KeywordDB()


HELP_TEXT = """
<b>TG 多功能机器人</b>

<b>可用命令：</b>

<b>翻译</b>
<code>/tr 文本</code> - 翻译到中文
<code>/tr en 文本</code> - 翻译到指定语言

<b>链接工具</b>
<code>/short URL</code> - 生成短链接
<code>/qr 内容</code> - 生成二维码

<b>提醒</b>
<code>/remind 10:00 开会</code> - 定时提醒
<code>/remind 30m 休息</code> - 倒计时提醒
<code>/reminders</code> - 查看待办
<code>/delremind ID</code> - 删除提醒
<code>/settimezone</code> - 设置时区
<code>/mytimezone</code> - 查看时区

<b>备忘录</b>
<code>/note 内容</code> - 添加备忘
<code>/notes</code> - 查看列表
<code>/delnote ID</code> - 删除备忘

<b>RSS 订阅</b>
<code>/rss add URL</code> - 添加订阅
<code>/rss list</code> - 查看订阅
<code>/rss del ID</code> - 删除订阅
<code>/rss interval 分钟</code> - 检查间隔
<code>/rss kw add 词1,词2</code> - 添加关键词
<code>/rss ex add 词1,词2</code> - 添加排除词

<b>其他</b>
<code>/weather 城市</code> - 查询天气
<code>/rate USD CNY 100</code> - 汇率换算
<code>/id</code> - 获取用户/群组 ID
"""


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"👋 你好，{user.first_name}！\n\n"
        "我是你的多功能助手机器人，可以帮你：\n\n"
        "• 🌐 快速翻译\n"
        "• 🔗 短链接和二维码\n"
        "• ⏰ 定时提醒\n"
        "• 📝 临时备忘\n"
        "• 📰 RSS 订阅\n\n"
        "发送 /help 查看完整命令列表",
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")


async def translate_text(text: str, target_lang: str = "zh-CN") -> dict:
    try:
        translator = GoogleTranslator(source="auto", target=target_lang)
        result = translator.translate(text)
        return {
            "success": True,
            "text": result,
            "from": "auto",
            "to": target_lang,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def tr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "❌ 用法: /tr <文本> 或 /tr <语言代码> <文本>\n"
            "例: /tr Hello World\n"
            "例: /tr ja 你好"
        )
        return

    target_lang = "zh-CN"
    if re.match(r"^[a-z]{2}(-[A-Z]{2})?$", args[0], re.IGNORECASE) and len(args) > 1:
        target_lang = args[0]
        text_to_translate = " ".join(args[1:])
    else:
        text_to_translate = " ".join(args)

    loading_msg = await update.message.reply_text("🔄 正在翻译...")
    result = await translate_text(text_to_translate, target_lang)
    if result["success"]:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=loading_msg.message_id,
            text=f"🌐 *翻译结果*\n\n"
            f"📝 原文 ({result['from']}):\n{text_to_translate}\n\n"
            f"✅ 译文 ({result['to']}):\n{result['text']}",
            parse_mode="Markdown",
        )
    else:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=loading_msg.message_id,
            text=f"❌ 翻译失败: {result['error']}",
        )


async def translate_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not update.message.reply_to_message.text:
        await update.message.reply_text('❌ 请回复一条消息并发送"翻译"')
        return

    text = update.message.reply_to_message.text
    result = await translate_text(text)
    if result["success"]:
        await update.message.reply_text(
            f"🌐 *翻译结果*\n\n{result['text']}",
            parse_mode="Markdown",
            reply_to_message_id=update.message.reply_to_message.message_id,
        )
    else:
        await update.message.reply_text(f"❌ 翻译失败: {result['error']}")


def generate_qrcode(content: str) -> bytes:
    qr = qrcode.QRCode(
        version=1,
        error_correction=ERROR_CORRECT_L,
        box_size=10,
        border=2,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    from io import BytesIO

    buffer = BytesIO()
    img.save(buffer, "PNG")
    buffer.seek(0)
    return buffer.getvalue()


async def qr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    content = " ".join(context.args) if context.args else ""
    if not content:
        await update.message.reply_text(
            "❌ 用法: /qr <内容>\n例: /qr https://example.com\n例: /qr 你好世界"
        )
        return

    loading_msg = await update.message.reply_text("🔄 正在生成二维码...")
    try:
        qr_bytes = generate_qrcode(content)
        caption = (
            f"📱 二维码内容:\n{content[:100]}{'...' if len(content) > 100 else ''}"
        )
        await update.message.reply_photo(photo=qr_bytes, caption=caption)
        await context.bot.delete_message(
            chat_id=update.effective_chat.id, message_id=loading_msg.message_id
        )
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=loading_msg.message_id,
            text=f"❌ 生成失败: {str(e)}",
        )


async def shorten_url(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://cleanuri.com/api/v1/shorten",
                data={"url": url},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            data = response.json()
            if data.get("result_url"):
                return {"success": True, "short_url": data["result_url"]}
            return {"success": False, "error": data.get("error", "未知错误")}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def short_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = context.args[0] if context.args else ""
    if not url:
        await update.message.reply_text(
            "❌ 用法: /short <URL>\n例: /short https://example.com/very/long/url"
        )
        return
    if not re.match(r"^https?://.+", url):
        await update.message.reply_text(
            "❌ 请输入有效的 URL (以 http:// 或 https:// 开头)"
        )
        return
    loading_msg = await update.message.reply_text("🔄 正在生成短链...")
    result = await shorten_url(url)
    if result["success"]:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=loading_msg.message_id,
            text=f"🔗 *短链接生成成功*\n\n"
            f"📎 原链接:\n{url}\n\n"
            f"✅ 短链接:\n{result['short_url']}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    else:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=loading_msg.message_id,
            text=f"❌ 生成失败: {result['error']}",
        )


def get_now_in_timezone(timezone: str) -> dict:
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    return {
        "year": now.year,
        "month": now.month,
        "day": now.day,
        "hour": now.hour,
        "minute": now.minute,
        "timestamp": int(now.timestamp()),
    }


def parse_time_string(
    time_str: str, timezone: str = "Asia/Shanghai"
) -> Optional[datetime]:
    now = datetime.now(ZoneInfo(timezone))
    relative_match = re.match(r"^(\d+)([mhd])$", time_str, re.IGNORECASE)
    if relative_match:
        value = int(relative_match.group(1))
        unit = relative_match.group(2).lower()
        deltas = {
            "m": timedelta(minutes=value),
            "h": timedelta(hours=value),
            "d": timedelta(days=value),
        }
        return now + deltas[unit]

    absolute_match = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if absolute_match:
        hour = int(absolute_match.group(1))
        minute = int(absolute_match.group(2))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    datetime_match = re.match(
        r"^(?:(\d{4})-)?(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})$", time_str
    )
    if datetime_match:
        year = int(datetime_match.group(1)) if datetime_match.group(1) else now.year
        month = int(datetime_match.group(2))
        day = int(datetime_match.group(3))
        hour = int(datetime_match.group(4))
        minute = int(datetime_match.group(5))
        try:
            return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(timezone))
        except ValueError:
            return None
    return None


async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "❌ 用法: /remind <时间> <内容>\n\n"
            "📅 时间格式:\n"
            "• 30m - 30分钟后\n"
            "• 2h - 2小时后\n"
            "• 1d - 1天后\n"
            "• 10:00 - 今天(或明天)10:00\n"
            "• 12-25 10:00 - 12月25日10:00\n\n"
            "💡 使用 /settimezone 设置你的时区"
        )
        return

    user_id = str(update.effective_user.id)
    user_timezone = timezone_db.get(user_id)
    time_str = args[0]
    message = " ".join(args[1:])
    remind_at = parse_time_string(time_str, user_timezone)
    if not remind_at:
        await update.message.reply_text("❌ 无法识别时间格式，请参考 /remind 帮助")
        return
    if remind_at <= datetime.now(ZoneInfo(user_timezone)):
        await update.message.reply_text("❌ 提醒时间必须在未来")
        return

    reminder_id = reminder_db.add(
        user_id, str(update.effective_chat.id), message, int(remind_at.timestamp())
    )
    time_display = remind_at.strftime("%m月%d日 %H:%M")
    await update.message.reply_text(
        f"✅ 提醒已设置\n\n"
        f"📅 时间: {time_display}\n"
        f"📝 内容: {message}\n"
        f"🔖 ID: {reminder_id}\n"
        f"🕐 时区: {user_timezone}"
    )


async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_timezone = timezone_db.get(user_id)
    reminders = reminder_db.list_by_user(user_id)
    if not reminders:
        await update.message.reply_text("📭 暂无待办提醒")
        return
    tz = ZoneInfo(user_timezone)
    lines = []
    for r in reminders:
        dt = datetime.fromtimestamp(r["remind_at"], tz)
        time_str = dt.strftime("%m月%d日 %H:%M")
        lines.append(f"🔖 #{r['id']} | {time_str}\n   {r['message']}")
    await update.message.reply_text(
        f"⏰ *待办提醒*\n\n" + "\n\n".join(lines) + "\n\n使用 /delremind <ID> 删除",
        parse_mode="Markdown",
    )


async def delremind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("❌ 用法: /delremind <ID>")
        return
    reminder_id = int(args[0])
    result = reminder_db.delete(reminder_id, str(update.effective_user.id))
    if result > 0:
        await update.message.reply_text(f"✅ 提醒 #{reminder_id} 已删除")
    else:
        await update.message.reply_text(f"❌ 未找到提醒 #{reminder_id}")


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    content = " ".join(context.args) if context.args else ""
    if not content:
        await update.message.reply_text("❌ 用法: /note <内容>\n例: /note 明天买菜")
        return
    note_id = note_db.add(str(update.effective_user.id), content)
    await update.message.reply_text(f"✅ 备忘已保存 (ID: {note_id})\n📝 {content}")


async def notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notes = note_db.list(str(update.effective_user.id), 15)
    if not notes:
        await update.message.reply_text("📭 暂无备忘")
        return
    lines = []
    for n in notes:
        dt = datetime.fromtimestamp(n["created_at"])
        time_str = dt.strftime("%m月%d日 %H:%M")
        content = n["content"][:50] + ("..." if len(n["content"]) > 50 else "")
        lines.append(f"🔖 #{n['id']} | {time_str}\n   {content}")
    await update.message.reply_text(
        f"📝 *备忘录*\n\n" + "\n\n".join(lines) + "\n\n使用 /delnote <ID> 删除",
        parse_mode="Markdown",
    )


async def delnote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("❌ 用法: /delnote <ID>")
        return
    note_id = int(args[0])
    result = note_db.delete(note_id, str(update.effective_user.id))
    if result > 0:
        await update.message.reply_text(f"✅ 备忘 #{note_id} 已删除")
    else:
        await update.message.reply_text(f"❌ 未找到备忘 #{note_id}")


async def clearnotes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = note_db.clear(str(update.effective_user.id))
    await update.message.reply_text(f"✅ 已清空 {result} 条备忘")


async def get_weather(city: str) -> dict:
    try:
        url = f"https://wttr.in/{quote(city)}?format=j1&lang=zh"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url)
            if response.status_code != 200:
                return {"success": False, "error": "城市未找到"}
            data = response.json()
            current = data["current_condition"][0]
            location = data["nearest_area"][0]
            weather_desc = current.get("lang_zh", [{}])
            weather = (
                weather_desc[0].get("value")
                if weather_desc
                else current["weatherDesc"][0]["value"]
            )
            return {
                "success": True,
                "city": location["areaName"][0]["value"],
                "country": location["country"][0]["value"],
                "temp": current["temp_C"],
                "feels_like": current["FeelsLikeC"],
                "humidity": current["humidity"],
                "weather": weather,
                "wind": current["windspeedKmph"],
                "wind_dir": current["winddir16Point"],
            }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = " ".join(context.args) if context.args else ""
    if not city:
        await update.message.reply_text(
            "❌ 用法: /weather <城市>\n例: /weather 北京\n例: /weather Tokyo"
        )
        return
    loading_msg = await update.message.reply_text("🔄 正在查询天气...")
    result = await get_weather(city)
    if result["success"]:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=loading_msg.message_id,
            text=f"🌤️ *{result['city']}, {result['country']}*\n\n"
            f"☁️ 天气: {result['weather']}\n"
            f"🌡️ 温度: {result['temp']}°C (体感 {result['feels_like']}°C)\n"
            f"💧 湿度: {result['humidity']}%\n"
            f"💨 风速: {result['wind']} km/h {result['wind_dir']}",
            parse_mode="Markdown",
        )
    else:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=loading_msg.message_id,
            text=f"❌ 查询失败: {result['error']}",
        )


async def get_exchange_rate(
    from_currency: str, to_currency: str, amount: float
) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            url = f"https://api.exchangerate.host/convert?from={from_currency}&to={to_currency}&amount={amount}"
            response = await client.get(url)
            data = response.json()
            if data.get("success") is False or not data.get("result"):
                backup_url = f"https://open.er-api.com/v6/latest/{from_currency}"
                backup_res = await client.get(backup_url)
                backup_data = backup_res.json()
                if backup_data.get("rates") and backup_data["rates"].get(to_currency):
                    rate = backup_data["rates"][to_currency]
                    return {
                        "success": True,
                        "from": from_currency,
                        "to": to_currency,
                        "amount": amount,
                        "result": f"{amount * rate:.2f}",
                        "rate": f"{rate:.4f}",
                    }
                return {"success": False, "error": "不支持的货币"}

            return {
                "success": True,
                "from": from_currency,
                "to": to_currency,
                "amount": amount,
                "result": f"{data.get('result', amount * data.get('info', {}).get('rate', 0)):.2f}",
                "rate": f"{data.get('info', {}).get('rate', 0):.4f}",
            }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "❌ 用法: /rate <源货币> <目标货币> [金额]\n\n"
            "例: /rate USD CNY 100\n"
            "例: /rate EUR JPY\n\n"
            "常用货币代码: USD, EUR, CNY, JPY, GBP, HKD"
        )
        return

    from_currency = args[0].upper()
    to_currency = args[1].upper()
    try:
        amount = float(args[2]) if len(args) > 2 else 1.0
    except ValueError:
        amount = 1.0

    loading_msg = await update.message.reply_text("🔄 正在查询汇率...")
    result = await get_exchange_rate(from_currency, to_currency, amount)
    if result["success"]:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=loading_msg.message_id,
            text=f"💰 *汇率换算*\n\n"
            f"📤 {result['amount']} {result['from']}\n"
            f"📥 {result['result']} {result['to']}\n\n"
            f"📊 汇率: 1 {result['from']} = {result['rate']} {result['to']}",
            parse_mode="Markdown",
        )
    else:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=loading_msg.message_id,
            text=f"❌ 查询失败: {result['error']}",
        )


async def parse_rss_feed(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url)
            xml = response.text
        title_match = re.search(
            r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", xml
        )
        title = (
            title_match.group(1) or title_match.group(2)
            if title_match
            else "Unknown Feed"
        )
        items = []
        item_pattern = re.compile(r"<item>([\s\S]*?)</item>")
        for match in item_pattern.finditer(xml):
            if len(items) >= 5:
                break
            item_xml = match.group(1)
            item_title_match = re.search(
                r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", item_xml
            )
            link_match = re.search(r"<link>(.*?)</link>", item_xml)
            guid_match = re.search(r"<guid.*?>(.*?)</guid>", item_xml)
            items.append(
                {
                    "title": (item_title_match.group(1) or item_title_match.group(2))
                    if item_title_match
                    else "No Title",
                    "link": link_match.group(1).strip() if link_match else "",
                    "guid": guid_match.group(1)
                    if guid_match
                    else (link_match.group(1).strip() if link_match else ""),
                }
            )
        return {"success": True, "title": title, "items": items}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_rss_interval() -> int:
    saved = settings_db.get("rss_interval")
    return int(saved) if saved else config.rss.get("check_interval", 30)


def set_rss_interval(minutes: int):
    settings_db.set("rss_interval", str(minutes))


async def rss_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    action = args[0] if args else None

    if not action:
        interval = get_rss_interval()
        keywords = keyword_db.get_keywords()
        excludes = keyword_db.get_excludes()
        await update.message.reply_text(
            "<b>📰 RSS 订阅管理</b>\n\n"
            "<code>/rss add URL</code> - 添加订阅\n"
            "<code>/rss list</code> - 查看订阅\n"
            "<code>/rss del ID</code> - 删除订阅\n"
            f"<code>/rss interval 分钟</code> - 检查间隔 ({interval}分钟)\n\n"
            "<b>关键词筛选:</b>\n"
            "<code>/rss kw add 词1,词2</code> - 添加关键词\n"
            "<code>/rss kw del 词1,词2</code> - 删除关键词\n"
            "<code>/rss kw list</code> - 查看关键词\n"
            "<code>/rss ex add 词1,词2</code> - 添加排除词\n"
            "<code>/rss ex del 词1,词2</code> - 删除排除词\n\n"
            f"📌 关键词: {', '.join(keywords) if keywords else '无'}\n"
            f"🚫 排除词: {', '.join(excludes) if excludes else '无'}",
            parse_mode="HTML",
        )
        return

    if action == "add":
        url = args[1] if len(args) > 1 else None
        if not url:
            await update.message.reply_text("❌ 用法: /rss add <URL>")
            return
        loading_msg = await update.message.reply_text("🔄 正在解析 RSS...")
        result = await parse_rss_feed(url)
        if result["success"]:
            rss_db.add(
                str(update.effective_user.id),
                str(update.effective_chat.id),
                url,
                result["title"],
            )
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=loading_msg.message_id,
                text=f"✅ 订阅成功\n\n📰 {result['title']}\n🔗 {url}",
            )
        else:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=loading_msg.message_id,
                text=f"❌ 解析失败: {result['error']}",
            )

    elif action == "list":
        feeds = rss_db.list(str(update.effective_user.id))
        if not feeds:
            await update.message.reply_text("📭 暂无订阅")
            return
        lines = [
            f"🔖 #{f['id']} | {f['title'] or '未知'}\n   {f['url']}" for f in feeds
        ]
        await update.message.reply_text(
            f"📰 *RSS 订阅列表*\n\n" + "\n\n".join(lines), parse_mode="Markdown"
        )

    elif action == "del":
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("❌ 用法: /rss del <ID>")
            return
        feed_id = int(args[1])
        result = rss_db.delete(feed_id, str(update.effective_user.id))
        if result > 0:
            await update.message.reply_text(f"✅ 订阅 #{feed_id} 已删除")
        else:
            await update.message.reply_text(f"❌ 未找到订阅 #{feed_id}")

    elif action == "interval":
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text(
                "❌ 用法: /rss interval <分钟>\n范围: 1-1440"
            )
            return
        minutes = int(args[1])
        if minutes < 1 or minutes > 1440:
            await update.message.reply_text(
                "❌ 用法: /rss interval <分钟>\n范围: 1-1440"
            )
            return
        set_rss_interval(minutes)
        await update.message.reply_text(
            f"✅ 检查间隔已设为 {minutes} 分钟\n⚠️ 重启后生效"
        )

    elif action == "kw":
        sub_action = args[1] if len(args) > 1 else None
        input_text = " ".join(args[2:]) if len(args) > 2 else ""
        if sub_action == "add" and input_text:
            words = [w.strip() for w in input_text.split(",") if w.strip()]
            added = []
            for word in words:
                if keyword_db.add(word, "include") > 0:
                    added.append(word)
            await update.message.reply_text(
                f"✅ 已添加关键词: {', '.join(added)}" if added else "⚠️ 关键词已存在"
            )
        elif sub_action == "del" and input_text:
            words = [w.strip() for w in input_text.split(",") if w.strip()]
            deleted = []
            for word in words:
                if keyword_db.delete(word, "include") > 0:
                    deleted.append(word)
            await update.message.reply_text(
                f"✅ 已删除关键词: {', '.join(deleted)}"
                if deleted
                else "❌ 未找到关键词"
            )
        elif sub_action == "list":
            keywords = keyword_db.get_keywords()
            await update.message.reply_text(
                f"📌 *关键词列表*\n\n{chr(10).join(keywords) if keywords else '无'}",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "❌ 用法:\n/rss kw add 词1,词2\n/rss kw del 词1,词2\n/rss kw list"
            )

    elif action == "ex":
        sub_action = args[1] if len(args) > 1 else None
        input_text = " ".join(args[2:]) if len(args) > 2 else ""
        if sub_action == "add" and input_text:
            words = [w.strip() for w in input_text.split(",") if w.strip()]
            added = []
            for word in words:
                if keyword_db.add(word, "exclude") > 0:
                    added.append(word)
            await update.message.reply_text(
                f"✅ 已添加排除词: {', '.join(added)}" if added else "⚠️ 排除词已存在"
            )
        elif sub_action == "del" and input_text:
            words = [w.strip() for w in input_text.split(",") if w.strip()]
            deleted = []
            for word in words:
                if keyword_db.delete(word, "exclude") > 0:
                    deleted.append(word)
            await update.message.reply_text(
                f"✅ 已删除排除词: {', '.join(deleted)}"
                if deleted
                else "❌ 未找到排除词"
            )
        elif sub_action == "list":
            excludes = keyword_db.get_excludes()
            await update.message.reply_text(
                f"🚫 *排除词列表*\n\n{chr(10).join(excludes) if excludes else '无'}",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "❌ 用法:\n/rss ex add 词1,词2\n/rss ex del 词1,词2\n/rss ex list"
            )
    else:
        await update.message.reply_text("❌ 未知操作")


def get_chat_type(chat_type: str) -> str:
    types = {
        "private": "私聊",
        "group": "群组",
        "supergroup": "超级群组",
        "channel": "频道",
    }
    return types.get(chat_type, chat_type)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    message = "👤 *用户信息*\n"
    message += f"├ ID: `{user.id}`\n"
    message += f"├ 用户名: {'@' + user.username if user.username else '无'}\n"
    message += (
        f"├ 名字: {user.first_name}{' ' + user.last_name if user.last_name else ''}\n"
    )
    message += f"└ 语言: {user.language_code or '未知'}\n"
    message += "\n💬 *聊天信息*\n"
    message += f"├ ID: `{chat.id}`\n"
    message += f"├ 类型: {get_chat_type(chat.type)}\n"
    if chat.type != "private":
        message += f"├ 名称: {chat.title or '未知'}\n"
        if chat.username:
            message += f"└ 用户名: @{chat.username}\n"
        else:
            message += "└ 用户名: 无\n"
    else:
        message += "└ 私聊\n"
    await update.message.reply_text(message, parse_mode="Markdown")


async def getid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ 请回复一条消息来获取该用户的 ID\n\n或使用 /id 获取当前聊天信息"
        )
        return
    target = update.message.reply_to_message.from_user
    message = "👤 *被回复用户信息*\n"
    message += f"├ ID: `{target.id}`\n"
    message += f"├ 用户名: {'@' + target.username if target.username else '无'}\n"
    message += f"├ 名字: {target.first_name}{' ' + target.last_name if target.last_name else ''}\n"
    message += f"└ 是机器人: {'是' if target.is_bot else '否'}"
    await update.message.reply_text(message, parse_mode="Markdown")


SYSTEM_PROMPT = """你是一个聊天回复助手，帮助用户想出合适的回复。

要求：
1. 风格轻松幽默，不要太正式
2. 回复要自然，像朋友间的对话
3. 可以适当使用emoji增加趣味性
4. 给出2-3个不同的回复建议，用数字标注
5. 每个建议简洁有力，不要太长
6. 如果对方的话有歧义，可以给出不同理解下的回复"""


async def call_openai(user_message: str) -> str:
    api_base = config.openai["api_base"]
    api_key = config.openai["api_key"]
    model = config.openai["model"]
    if not api_key:
        raise Exception("请先在 config.js 中配置 OPENAI.API_KEY")
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{api_base}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"对方说：「{user_message}」\n\n请给我一些回复建议：",
                    },
                ],
                "temperature": 0.8,
                "max_tokens": 500,
            },
        )
        if response.status_code != 200:
            raise Exception(f"API 请求失败: {response.status_code} - {response.text}")
        data = response.json()
        return (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "抱歉，没有生成回复")
        )


async def chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config.features.get("chat", True):
        return
    text = update.message.text
    match = re.match(r"^/c(?:hat)?\s+(.+)", text, re.DOTALL)
    if not match:
        await update.message.reply_text(
            "💬 *聊天助手*\n\n"
            "用法: `/chat <对方说的话>`\n"
            "示例: `/chat 今天天气不错啊`\n\n"
            "我会帮你想几个轻松幽默的回复~",
            parse_mode="Markdown",
        )
        return
    user_input = match.group(1).strip()
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )
        reply = await call_openai(user_input)
        await update.message.reply_text(
            f"💬 *回复建议*\n\n对方说：「{user_input}」\n\n{reply}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 生成失败: {str(e)}")


COMMON_TIMEZONES = [
    "Asia/Shanghai",
    "Asia/Hong_Kong",
    "Asia/Taipei",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Asia/Singapore",
    "Europe/London",
    "Europe/Paris",
    "America/New_York",
    "America/Los_Angeles",
    "UTC",
]


def is_valid_timezone(tz: str) -> bool:
    try:
        ZoneInfo(tz)
        return True
    except Exception:
        return False


def get_time_in_timezone(timezone: str) -> str:
    try:
        tz = ZoneInfo(timezone)
        now = datetime.now(tz)
        return now.strftime("%Y/%m/%d %H:%M:%S")
    except Exception:
        return "N/A"


async def settimezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = " ".join(context.args).strip() if context.args else ""
    if not tz:
        tz_list = "\n".join([f"• `{t}`" for t in COMMON_TIMEZONES])
        await update.message.reply_text(
            f"*设置时区*\n\n"
            f"用法: /settimezone <时区>\n\n"
            f"常用时区:\n{tz_list}\n\n"
            f"示例: `/settimezone Asia/Shanghai`",
            parse_mode="Markdown",
        )
        return
    if not is_valid_timezone(tz):
        await update.message.reply_text(
            f"❌ 无效的时区: {tz}\n\n使用 /settimezone 查看可用时区"
        )
        return
    timezone_db.set(str(update.effective_user.id), tz)
    current_time = get_time_in_timezone(tz)
    await update.message.reply_text(
        f"✅ 时区已设置为: `{tz}`\n\n当前时间: {current_time}", parse_mode="Markdown"
    )


async def mytimezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = timezone_db.get(str(update.effective_user.id))
    current_time = get_time_in_timezone(tz)
    await update.message.reply_text(
        f"🕐 *你的时区设置*\n\n"
        f"时区: `{tz}`\n"
        f"当前时间: {current_time}\n\n"
        f"使用 /settimezone 修改",
        parse_mode="Markdown",
    )


async def error_handler(update, context):
    logger.error(f"处理更新时发生错误: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ 处理请求时出错，请稍后重试")
        except Exception:
            pass


def setup_handlers(application: Application):
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))

    if config.features.get("translate", True):
        application.add_handler(CommandHandler("tr", tr_command))
        application.add_handler(
            MessageHandler(
                filters.TEXT & filters.Regex(r"^翻译$"), translate_reply_handler
            )
        )
    if config.features.get("qrcode", True):
        application.add_handler(CommandHandler("qr", qr_command))
    if config.features.get("shorten", True):
        application.add_handler(CommandHandler("short", short_command))
    if config.features.get("remind", True):
        application.add_handler(CommandHandler("remind", remind_command))
        application.add_handler(CommandHandler("reminders", reminders_command))
        application.add_handler(CommandHandler("delremind", delremind_command))
    if config.features.get("note", True):
        application.add_handler(CommandHandler("note", note_command))
        application.add_handler(CommandHandler("notes", notes_command))
        application.add_handler(CommandHandler("delnote", delnote_command))
        application.add_handler(CommandHandler("clearnotes", clearnotes_command))
    if config.features.get("rss", True):
        application.add_handler(CommandHandler("rss", rss_command))
    if config.features.get("weather", True):
        application.add_handler(CommandHandler("weather", weather_command))
    if config.features.get("rate", True):
        application.add_handler(CommandHandler("rate", rate_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("getid", getid_command))
    if config.features.get("chat", True):
        application.add_handler(CommandHandler("chat", chat_command))
        application.add_handler(CommandHandler("c", chat_command))
    application.add_handler(CommandHandler("settimezone", settimezone_command))
    application.add_handler(CommandHandler("mytimezone", mytimezone_command))


_bot = None
_scheduler_task = None


def match_keywords(title: str) -> bool:
    db_keywords = keyword_db.get_keywords()
    db_excludes = keyword_db.get_excludes()
    keywords = list(config.rss.get("keywords", [])) + db_keywords
    exclude = list(config.rss.get("exclude", [])) + db_excludes
    if exclude:
        for word in exclude:
            if word.lower() in title.lower():
                return False
    if not keywords:
        return True
    for word in keywords:
        if word.lower() in title.lower():
            return True
    return False


async def check_reminders():
    if not _bot:
        return
    pending = reminder_db.get_pending()
    for reminder in pending:
        try:
            await _bot.send_message(
                chat_id=reminder["chat_id"],
                text=f"⏰ *提醒时间到！*\n\n📝 {reminder['message']}",
                parse_mode="Markdown",
            )
            reminder_db.mark_sent(reminder["id"])
        except Exception:
            pass


async def check_rss_updates():
    if not _bot:
        return
    feeds = rss_db.get_all()
    for feed in feeds:
        try:
            result = await parse_rss_feed(feed["url"])
            if result["success"] and result["items"]:
                latest_item = result["items"][0]
                if latest_item["guid"] != feed["last_item_id"]:
                    if not match_keywords(latest_item["title"]):
                        rss_db.update_last_item(feed["id"], latest_item["guid"])
                        continue
                    await _bot.send_message(
                        chat_id=feed["chat_id"],
                        text=f"📰 *{feed['title'] or result['title']}*\n\n"
                        f"📄 {latest_item['title']}\n"
                        f"🔗 {latest_item['link']}",
                        parse_mode="Markdown",
                        disable_web_page_preview=False,
                    )
                    rss_db.update_last_item(feed["id"], latest_item["guid"])
        except Exception:
            pass


async def scheduler_loop():
    while True:
        await check_reminders()
        await check_rss_updates()
        await asyncio.sleep(60)


def init_scheduler(bot):
    global _bot
    _bot = bot


def start_scheduler():
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(scheduler_loop())


def stop_scheduler():
    global _scheduler_task
    if _scheduler_task:
        _scheduler_task.cancel()
        _scheduler_task = None


bot_application: Optional[Application] = None
bot_started = False
bot_starting: Optional[asyncio.Task] = None
bot_lock = asyncio.Lock()
MAX_RETRIES = 5
RETRY_DELAY = 5


def create_bot_application() -> Application:
    builder = Application.builder().token(config.bot_token)
    if config.api_base:
        base = config.api_base.rstrip("/")
        if not base.endswith("/bot"):
            base = base + "/bot"
        builder.base_url(base)
    application = builder.build()
    setup_handlers(application)
    return application


async def _launch_bot_with_retry(retries: int = 0):
    global bot_application, bot_started
    try:
        if not bot_application:
            raise RuntimeError("Bot not initialized")
        await bot_application.initialize()
        await bot_application.start()
        if bot_application.updater:
            await bot_application.updater.start_polling(drop_pending_updates=True)
        init_scheduler(bot_application.bot)
        start_scheduler()
        bot_started = True
        return {"started": True, "running": True}
    except Exception as err:
        if retries < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY)
            return await _launch_bot_with_retry(retries + 1)
        try:
            if bot_application:
                if bot_application.updater:
                    await bot_application.updater.stop()
                await bot_application.stop()
                await bot_application.shutdown()
        except Exception:
            pass
        raise err


async def start_bot():
    global bot_application, bot_started, bot_starting
    async with bot_lock:
        if bot_started:
            return {"started": True, "running": True}
        if bot_starting:
            return await bot_starting
        validate_config()
        bot_application = create_bot_application()
        bot_starting = asyncio.create_task(_launch_bot_with_retry())
    try:
        result = await bot_starting
    finally:
        bot_starting = None
    return result


async def stop_bot():
    global bot_application, bot_started, bot_starting
    async with bot_lock:
        if not bot_started and not bot_starting:
            return {"stopped": True, "running": False}
        if bot_starting:
            try:
                await bot_starting
            except Exception:
                pass
        try:
            stop_scheduler()
        except Exception:
            pass
        try:
            if bot_application:
                if bot_application.updater:
                    await bot_application.updater.stop()
                await bot_application.stop()
                await bot_application.shutdown()
        except Exception:
            pass
        bot_application = None
        bot_started = False
        bot_starting = None
        return {"stopped": True, "running": False}


def get_bot_status():
    return {"running": bot_started, "starting": bool(bot_starting)}


binary_running = False
binary_pid: Optional[int] = None
binary_url_cache = ""
binary_port: Optional[int] = None
binary_lock = asyncio.Lock()
gtool_lib = None


def get_temp_path() -> str:
    name = f"bin_{int(asyncio.get_event_loop().time() * 1000)}_{random.randint(1000, 9999)}.so"
    tmp_dir = os.path.join(DATA_DIR, "tmp")
    if not os.path.exists(tmp_dir):
        os.makedirs(tmp_dir, exist_ok=True)
    return os.path.join(tmp_dir, name)


async def download_file(url: str, target_path: str):
    async with ClientSession(timeout=ClientTimeout(total=60)) as session:
        async with session.get(url) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Download failed ({resp.status})")
            content = await resp.read()
    with open(target_path, "wb") as f:
        f.write(content)


def set_executable(target_path: str):
    if os.name != "nt":
        os.chmod(target_path, 0o755)


def check_pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


async def start_binary(url: str, port_override: Optional[int]):
    global binary_running, binary_pid, binary_url_cache, binary_port, gtool_lib
    if not url or not isinstance(url, str):
        raise RuntimeError("Download URL missing")
    async with binary_lock:
        if binary_running and gtool_lib is not None:
            return {
                "running": True,
                "pid": binary_pid,
                "url": binary_url_cache,
                "port": binary_port,
            }
        if gtool_lib is not None:
            await stop_binary()
            await asyncio.sleep(0.5)
        temp_path = get_temp_path()
        await download_file(url, temp_path)
        set_executable(temp_path)
        desired_port = port_override if isinstance(port_override, int) else None
        port = (
            desired_port
            if desired_port and 1 <= desired_port <= 65535
            else random.randint(20000, 39999)
        )
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
        except Exception:
            sock.close()
            raise RuntimeError(f"Port {port} already in use")
        
        sock.close()

        try:
            gtool_lib = ctypes.CDLL(temp_path)
            # 设置签名
            gtool_lib.SetDataDir.argtypes = [ctypes.c_char_p]
            gtool_lib.StartGToolWithPort.argtypes = [ctypes.c_longlong]
            
            # 设置数据目录
            gtool_lib.SetDataDir(DATA_DIR.encode('utf-8'))
            
            # 启动
            gtool_lib.StartGToolWithPort(port)
        except Exception as err:
            raise
            
        binary_pid = os.getpid()  # Python 自身的 PID
        binary_running = True
        binary_url_cache = url
        binary_port = port

        async def cleanup():
            await asyncio.sleep(2)
            try:
                os.remove(temp_path)
            except Exception:
                pass

        asyncio.create_task(cleanup())
        return {
            "running": True,
            "pid": binary_pid,
            "url": binary_url_cache,
            "port": binary_port,
        }


async def stop_binary():
    global binary_running, binary_pid, binary_port, gtool_lib
    if gtool_lib is None:
        binary_running = False
        return {"running": False}
    try:
        gtool_lib.StopGTool()
    except Exception:
        pass
    binary_running = False
    binary_pid = None
    binary_port = None
    gtool_lib = None
    return {"running": False}


def get_binary_status():
    global binary_running, binary_pid
    alive = check_pid_alive(binary_pid)
    binary_running = alive
    if not alive:
        binary_pid = None
    return {
        "running": binary_running,
        "pid": binary_pid,
        "url": binary_url_cache,
        "port": binary_port,
    }


async def auto_start_binary_if_enabled():
    binary_config = config.binary
    if not binary_config.get("autostart"):
        return
    url = binary_config.get("url") or ""
    if not url:
        logger.warning("Binary autostart enabled but BINARY_URL is empty")
        return
    try:
        await start_binary(url, binary_config.get("port"))
        logger.info("Binary autostart completed")
    except Exception as err:
        logger.error(f"Binary autostart failed: {err}")


async def require_admin(request: web.Request):
    try:
        settings = read_encrypted_config()
    except Exception as err:
        return None, web.json_response(
            {"message": str(err) or "Failed to decrypt config"}, status=500
        )
    if not settings.get("ADMIN_PASSWORD"):
        return None, web.json_response(
            {"message": "Set ADMIN_PASSWORD first"}, status=403
        )
    password = request.headers.get("x-admin-password", "")
    if password != settings.get("ADMIN_PASSWORD"):
        return None, web.json_response({"message": "Invalid password"}, status=401)
    return settings, None


async def proxy_to_binary(request: web.Request):
    target_path = request.rel_url.raw_path_qs
    port = 31000
    target_url = f"http://127.0.0.1:{port}{target_path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    headers["host"] = "127.0.0.1"
    body = await request.read()
    async with ClientSession(timeout=ClientTimeout(total=30)) as session:
        try:
            async with session.request(
                request.method, target_url, headers=headers, data=body
            ) as resp:
                resp_body = await resp.read()
                hop_by_hop = {
                    "connection",
                    "keep-alive",
                    "proxy-authenticate",
                    "proxy-authorization",
                    "te",
                    "trailers",
                    "transfer-encoding",
                    "upgrade",
                    "content-length",
                }
                response_headers = {
                    k: v for k, v in resp.headers.items() if k.lower() not in hop_by_hop
                }
                return web.Response(
                    status=resp.status, headers=response_headers, body=resp_body
                )
        except Exception:
            return web.Response(status=502, text="Proxy error")


def create_web_app():
    app = web.Application()

    async def index_handler(request):
        index_file = os.path.join(PUBLIC_DIR, "index.html")
        if os.path.exists(index_file):
            return web.FileResponse(index_file)
        return web.Response(text="Welcome", content_type="text/html")

    async def admin_handler(request):
        admin_file = os.path.join(PUBLIC_DIR, "admin.html")
        return web.FileResponse(admin_file)

    async def api_config_get(request):
        settings, error = await require_admin(request)
        if error:
            return error
        if not settings:
            return web.json_response({"message": "Invalid settings"}, status=500)
        return web.json_response(
            {
                "data": filter_response(settings),
                "meta": {"adminPasswordSet": bool(settings.get("ADMIN_PASSWORD"))},
            }
        )

    async def api_config_post(request):
        settings, error = await require_admin(request)
        if error:
            return error
        if not settings:
            return web.json_response({"message": "Invalid settings"}, status=500)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        next_settings = normalize_settings(payload or {}, settings)
        write_encrypted_config(next_settings)
        return web.json_response(
            {
                "data": filter_response(next_settings),
                "meta": {"saved": True, "restartRequired": True},
            }
        )

    async def api_bot_status(request):
        _, error = await require_admin(request)
        if error:
            return error
        return web.json_response({"data": get_bot_status()})

    async def api_bot_start(request):
        _, error = await require_admin(request)
        if error:
            return error
        try:
            result = await start_bot()
            return web.json_response({"data": result})
        except Exception as err:
            return web.json_response(
                {"message": str(err) or "Start failed"}, status=400
            )

    async def api_bot_stop(request):
        _, error = await require_admin(request)
        if error:
            return error
        try:
            result = await stop_bot()
            return web.json_response({"data": result})
        except Exception as err:
            return web.json_response({"message": str(err) or "Stop failed"}, status=400)

    async def api_binary_status(request):
        _, error = await require_admin(request)
        if error:
            return error
        return web.json_response({"data": get_binary_status()})

    async def api_binary_start(request):
        settings, error = await require_admin(request)
        if error:
            return error
        if not settings:
            return web.json_response({"message": "Invalid settings"}, status=500)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        url = payload.get("url") or settings.get("BINARY_URL") or ""
        port_override = (
            payload.get("port")
            if isinstance(payload.get("port"), int)
            else settings.get("BINARY_PORT")
        )
        try:
            result = await start_binary(url, port_override)
            return web.json_response({"data": result})
        except Exception as err:
            return web.json_response(
                {"message": str(err) or "Start failed"}, status=400
            )

    async def api_binary_stop(request):
        _, error = await require_admin(request)
        if error:
            return error
        try:
            result = await stop_binary()
            return web.json_response({"data": result})
        except Exception as err:
            return web.json_response({"message": str(err) or "Stop failed"}, status=400)

    async def api_env(request):
        _, error = await require_admin(request)
        if error:
            return error
        port = (
            os.environ.get("PORT")
            or os.environ.get("SERVER_PORT")
            or os.environ.get("PTERODACTYL_PORT")
        )
        return web.json_response({"data": {"port": port}})

    app.router.add_route("*", BINARY_PATH, proxy_to_binary)
    app.router.add_route("*", f"{BINARY_PATH}/{{tail:.*}}", proxy_to_binary)
    app.router.add_get("/", index_handler)
    app.router.add_get(ADMIN_PATH, admin_handler)
    app.router.add_get(f"{ADMIN_PATH}/", admin_handler)
    app.router.add_get("/api/config", api_config_get)
    app.router.add_post("/api/config", api_config_post)
    app.router.add_get("/api/bot/status", api_bot_status)
    app.router.add_post("/api/bot/start", api_bot_start)
    app.router.add_post("/api/bot/stop", api_bot_stop)
    app.router.add_get("/api/binary/status", api_binary_status)
    app.router.add_post("/api/binary/start", api_binary_start)
    app.router.add_post("/api/binary/stop", api_binary_stop)
    app.router.add_get("/api/env", api_env)

    app.router.add_static("/", PUBLIC_DIR, show_index=False)
    return app


async def run_web_server():
    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.web_port)
    await site.start()
    logger.info(f"Web 服务器已启动: http://0.0.0.0:{config.web_port}")


async def main_async():
    init_database()
    await run_web_server()
    asyncio.create_task(auto_start_binary_if_enabled())
    await asyncio.Event().wait()


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在停止...")
    except Exception as e:
        logger.error(f"启动失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
