"""Central configuration loaded from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

import pytz
from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Required environment variable {name!r} is not set")
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Telegram
TELEGRAM_TOKEN = _required("TELEGRAM_TOKEN")
TELEGRAM_WEBHOOK_URL = _optional("TELEGRAM_WEBHOOK_URL", "")
TELEGRAM_WEBHOOK_SECRET = _optional("TELEGRAM_WEBHOOK_SECRET", "")
TELEGRAM_GROUP_CHAT_ID = int(_optional("TELEGRAM_GROUP_CHAT_ID", "0"))

# Apple iCloud
ICLOUD_USER = _required("ICLOUD_USER")
ICLOUD_APP_PASSWORD = _required("ICLOUD_APP_PASSWORD")
ICLOUD_CALENDAR_NAME = _required("ICLOUD_CALENDAR_NAME")

# Gemini
GEMINI_API_KEY = _required("GEMINI_API_KEY")
GEMINI_MODEL = _optional("GEMINI_MODEL", "gemini-2.5-flash")

# Parent identity mapping
TAL_TELEGRAM_ID = int(_optional("TAL_TELEGRAM_ID", "0"))
BEN_TELEGRAM_ID = int(_optional("BEN_TELEGRAM_ID", "0"))

PARENT_MAP: dict[int, str] = {}
if TAL_TELEGRAM_ID:
    PARENT_MAP[TAL_TELEGRAM_ID] = "טל"
if BEN_TELEGRAM_ID:
    PARENT_MAP[BEN_TELEGRAM_ID] = "בן"

PARENTS = ["טל", "בן"]
CHILDREN = ["נועם", "עמית"]

# Timezone
TIMEZONE = pytz.timezone("Asia/Jerusalem")

# Database
DB_PATH = _optional("DB_PATH", "./data/family_calendar.db")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

# Scheduler
MORNING_HOUR = int(_optional("MORNING_HOUR", "7"))
MORNING_MINUTE = int(_optional("MORNING_MINUTE", "0"))

# Server
PORT = int(_optional("PORT", "8080"))
LOG_LEVEL = _optional("LOG_LEVEL", "INFO")

# Conversation state timeout (minutes)
STATE_TIMEOUT_MINUTES = 10
