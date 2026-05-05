"""Gemini 1.5 Flash wrapper for parsing Hebrew family-calendar messages into JSON.

Designed as a thin swappable interface — to switch providers, replace `parse_event_text`'s
LLM call. Keep the JSON contract identical.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import google.generativeai as genai

import config

logger = logging.getLogger(__name__)

genai.configure(api_key=config.GEMINI_API_KEY)
_model = genai.GenerativeModel(
    config.GEMINI_MODEL,
    generation_config={
        "temperature": 0.1,
        "response_mime_type": "application/json",
    },
)


@dataclass
class ParsedEvent:
    title: str
    start_time: datetime | None
    end_time: datetime | None
    is_recurring: bool
    recurrence_rule: str | None
    involves_children: list[str]
    mentioned_parents: list[str]
    intent: Literal["event", "task"]
    clarification_needed: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarification_needed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "is_recurring": self.is_recurring,
            "recurrence_rule": self.recurrence_rule,
            "involves_children": self.involves_children,
            "mentioned_parents": self.mentioned_parents,
            "intent": self.intent,
            "clarification_needed": self.clarification_needed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParsedEvent":
        return cls(
            title=data.get("title", ""),
            start_time=datetime.fromisoformat(data["start_time"]) if data.get("start_time") else None,
            end_time=datetime.fromisoformat(data["end_time"]) if data.get("end_time") else None,
            is_recurring=bool(data.get("is_recurring", False)),
            recurrence_rule=data.get("recurrence_rule"),
            involves_children=data.get("involves_children", []) or [],
            mentioned_parents=data.get("mentioned_parents", []) or [],
            intent=data.get("intent", "event"),
            clarification_needed=data.get("clarification_needed"),
        )


PROMPT_TEMPLATE = """אתה עוזר לניהול לוח שנה משפחתי. תפקידך להמיר הודעה בעברית ל-JSON מובנה.

הקשר:
- שולח ההודעה: {sender_name}
- שמות הילדים שיש לזהות: {children}
- שמות ההורים: {parents}
- זמן נוכחי (Asia/Jerusalem): {now}

כללים חשובים:
1. אם ההודעה משתמשת בכינוי גוף ראשון ("אני", "אקח", "אסיע", "אוביל"), הכוונה היא ל-{sender_name}.
2. כל זמן חייב להיות ב-ISO8601 עם אזור זמן +03:00 (Asia/Jerusalem).
3. אם לא צוינה שעת סיום, חשב 60 דקות מ-start_time.
4. אם מדובר במשימה ללא שעה ספציפית (למשל "לזכור לקנות חיתולים"), השתמש ב-intent="task" וקבע start_time לתאריך הרלוונטי בשעה 09:00.
5. אם תאריך/שעה לא ברורים — החזר {{"clarification_needed": "השאלה שלך בעברית"}} בלי שדות אחרים.
6. אם מוזכר ילד ({children}), כלול אותו ב-involves_children.
7. אם הוזכר במפורש שם של הורה ({parents}) או כינוי גוף ראשון מהשולח, כלול את שם ההורה ב-mentioned_parents. אחרת השאר רשימה ריקה.
8. אירועים חוזרים: מלא recurrence_rule כ-RRULE לפי RFC 5545 (לדוגמה: "FREQ=WEEKLY;BYDAY=SU").

החזר JSON בלבד, ללא טקסט נוסף, במבנה:
{{
  "title": "כותרת קצרה בעברית",
  "start_time": "2026-05-12T10:00:00+03:00",
  "end_time": "2026-05-12T11:00:00+03:00",
  "is_recurring": false,
  "recurrence_rule": null,
  "involves_children": [],
  "mentioned_parents": [],
  "intent": "event"
}}

או, במקרה של חוסר ודאות:
{{"clarification_needed": "באיזו שעה?"}}

הודעה לעיבוד:
{text}
"""


class LLMError(Exception):
    pass


class ParseError(Exception):
    pass


def _build_prompt(text: str, sender_name: str) -> str:
    now = datetime.now(config.TIMEZONE).isoformat()
    return PROMPT_TEMPLATE.format(
        sender_name=sender_name,
        children=", ".join(config.CHILDREN),
        parents=", ".join(config.PARENTS),
        now=now,
        text=text,
    )


def _strip_json(raw: str) -> str:
    """Strip markdown fences if present (Gemini occasionally adds them)."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    return raw.strip()


async def _call_gemini(prompt: str) -> str:
    def _sync_call() -> str:
        response = _model.generate_content(prompt)
        return response.text or ""

    try:
        return await asyncio.to_thread(_sync_call)
    except Exception as exc:
        logger.exception("Gemini call failed")
        raise LLMError(str(exc)) from exc


async def parse_event_text(text: str, sender_name: str) -> ParsedEvent:
    """Parse free-text Hebrew message into a ParsedEvent.

    Retries once with a correction hint if validation fails.
    """
    prompt = _build_prompt(text, sender_name)
    raw_response = await _call_gemini(prompt)

    try:
        data = json.loads(_strip_json(raw_response))
    except json.JSONDecodeError:
        retry_prompt = prompt + "\n\nתשובתך הקודמת לא הייתה JSON תקין. החזר JSON תקין בלבד."
        raw_response = await _call_gemini(retry_prompt)
        try:
            data = json.loads(_strip_json(raw_response))
        except json.JSONDecodeError as exc:
            raise ParseError(f"Gemini returned non-JSON twice: {raw_response!r}") from exc

    if "clarification_needed" in data and data["clarification_needed"]:
        return ParsedEvent(
            title="",
            start_time=None,
            end_time=None,
            is_recurring=False,
            recurrence_rule=None,
            involves_children=[],
            mentioned_parents=[],
            intent="event",
            clarification_needed=data["clarification_needed"],
            raw=data,
        )

    try:
        parsed = ParsedEvent.from_dict(data)
    except (KeyError, ValueError, TypeError) as exc:
        raise ParseError(f"Invalid ParsedEvent payload: {data!r}") from exc

    parsed.involves_children = [c for c in parsed.involves_children if c in config.CHILDREN]
    parsed.mentioned_parents = [p for p in parsed.mentioned_parents if p in config.PARENTS]
    if parsed.intent not in ("event", "task"):
        parsed.intent = "event"
    parsed.raw = data
    return parsed
