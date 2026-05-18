#!/usr/bin/env python3
"""Standalone morning routine — runs via GitHub Actions cron at 7am Israel time.

Reads all secrets from environment variables (fetched from GCP Secret Manager
by the workflow). Sends Telegram messages directly using the Bot API.
No dependency on the main bot container being healthy.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date as date_type
from datetime import datetime, timedelta

import caldav
import pytz
from caldav.elements import dav
from icalendar import Calendar as ICal
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# ── Config ────────────────────────────────────────────────────────────────────
TIMEZONE = pytz.timezone("Asia/Jerusalem")

TELEGRAM_TOKEN         = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_GROUP_CHAT_ID = int(os.environ.get("TELEGRAM_GROUP_CHAT_ID", "0"))
ICLOUD_USER            = os.environ["ICLOUD_USER"]
ICLOUD_APP_PASSWORD    = os.environ["ICLOUD_APP_PASSWORD"]
ICLOUD_CALENDAR_NAME   = os.environ.get("ICLOUD_CALENDAR_NAME", "Shared Calendar")

# Parent Apple ID emails — used to detect their events by organizer field
BEN_EMAILS = {"benariel@gmail.com", "benariel@icloud.com"}
TAL_EMAILS = {"tal8202@gmail.com", "tal8202@icloud.com"}

HEB_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]

MORNING_OPTIONS = [
    "טל מפזר/ת, בן אוסף/ת",
    "בן מפזר/ת, טל אוסף/ת",
    "טל גם מפזר/ת וגם אוסף/ת",
    "בן גם מפזר/ת וגם אוסף/ת",
]


# ── Keyboards ─────────────────────────────────────────────────────────────────
def kb_morning() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(opt, callback_data=f"m:{i}")]
        for i, opt in enumerate(MORNING_OPTIONS)
    ]
    return InlineKeyboardMarkup(rows)


# ── CalDAV helpers ────────────────────────────────────────────────────────────
def _ensure_aware(dt: datetime | date_type) -> datetime:
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else TIMEZONE.localize(dt)
    return TIMEZONE.localize(datetime.combine(dt, datetime.min.time()))


def _connect_calendar() -> caldav.Calendar:
    client = caldav.DAVClient(
        url="https://caldav.icloud.com/",
        username=ICLOUD_USER,
        password=ICLOUD_APP_PASSWORD,
    )
    principal = client.principal()
    target = ICLOUD_CALENDAR_NAME.strip()
    for cal in principal.calendars():
        try:
            props = cal.get_properties([dav.DisplayName()])
            name = props.get("{DAV:}displayname", "").strip()
        except Exception:
            name = (cal.name or "").strip()
        if name == target:
            print(f"Connected to calendar: {name!r}")
            return cal
    available = [c.name for c in principal.calendars()]
    raise RuntimeError(f"Calendar {target!r} not found. Available: {available}")


def _get_today_events(calendar: caldav.Calendar, today: date_type) -> list[dict]:
    start = _ensure_aware(today)
    end   = start + timedelta(days=1)
    results = calendar.date_search(start=start, end=end, expand=True)

    events: list[dict] = []
    for item in results:
        try:
            ical = ICal.from_ical(item.data)
        except Exception:
            continue
        for component in ical.walk("VEVENT"):
            dtstart = component.get("dtstart")
            dtend   = component.get("dtend")
            start_val = dtstart.dt if dtstart else None
            end_val   = dtend.dt   if dtend   else None
            if start_val is None:
                continue

            is_all_day = isinstance(start_val, date_type) and not isinstance(start_val, datetime)

            # Filter to today only
            if not is_all_day:
                s_local = _ensure_aware(start_val).astimezone(TIMEZONE)
                if s_local.date() != today:
                    continue

            # Organizer email
            organizer = component.get("organizer")
            organizer_email = (
                str(organizer).replace("mailto:", "").lower() if organizer else ""
            )

            events.append({
                "title":           str(component.get("summary", "ללא כותרת")),
                "start":           start_val,
                "end":             end_val,
                "is_all_day":      is_all_day,
                "organizer_email": organizer_email,
            })
    return events


# ── Logic ─────────────────────────────────────────────────────────────────────
def _check_overlap(events: list[dict]) -> bool:
    ben_slots, tal_slots = [], []
    for ev in events:
        if ev["is_all_day"] or not ev["start"]:
            continue
        s = _ensure_aware(ev["start"])
        e = _ensure_aware(ev["end"]) if ev["end"] else s + timedelta(hours=1)
        title = ev["title"]
        org   = ev["organizer_email"]
        # Match by bot-assigned title suffix OR by organizer email
        is_ben = ("- בן" in title) or (org in BEN_EMAILS)
        is_tal = ("- טל" in title) or (org in TAL_EMAILS)
        if is_ben:
            ben_slots.append((s, e))
        if is_tal:
            tal_slots.append((s, e))

    return any(
        bs < te and be > ts
        for bs, be in ben_slots
        for ts, te in tal_slots
    )


def _format_summary(events: list[dict], today: date_type) -> str:
    weekday = HEB_DAYS[today.weekday()]
    header  = f'בוקר טוב! הנה הלו"ז להיום 📅\n{weekday}, {today.strftime("%d/%m/%Y")}'
    timed   = [e for e in events if not e["is_all_day"] and e["start"]]
    allday  = [e for e in events if e["is_all_day"]]

    if not timed and not allday:
        return f"{header}\n\nאין אירועים מתוכננים להיום."

    lines = [header, ""]
    for e in allday:
        lines.append(f"📌 {e['title']} (כל היום)")
    for e in sorted(timed, key=lambda x: _ensure_aware(x["start"])):
        s  = _ensure_aware(e["start"])
        en = _ensure_aware(e["end"]) if e["end"] else s + timedelta(hours=1)
        lines.append(f"🕒 {s.strftime('%H:%M')}–{en.strftime('%H:%M')} | {e['title']}")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    today = datetime.now(TIMEZONE).date()
    print(f"Morning job starting for {today}")

    bot    = Bot(token=TELEGRAM_TOKEN)
    events: list[dict] = []

    # 1. Connect to iCloud (with timeout)
    try:
        calendar = await asyncio.wait_for(
            asyncio.to_thread(_connect_calendar), timeout=40.0
        )
        events = await asyncio.wait_for(
            asyncio.to_thread(_get_today_events, calendar, today), timeout=30.0
        )
        print(f"Fetched {len(events)} events for today")
    except asyncio.TimeoutError:
        print("CalDAV timeout — will send fallback greeting", file=sys.stderr)
    except Exception as exc:
        print(f"CalDAV error: {exc}", file=sys.stderr)

    async with bot:
        # 2. Parent overlap → childcare question
        try:
            if events and _check_overlap(events):
                print("Overlap detected — sending childcare question")
                await bot.send_message(
                    TELEGRAM_GROUP_CHAT_ID,
                    "🧒 שני ההורים עסוקים בו-זמנית היום — מי שומר על נועם ועמית?",
                )
        except Exception as exc:
            print(f"Overlap check error: {exc}", file=sys.stderr)

        # 3. Logistics question — skip on Saturday (weekday 5)
        if today.weekday() != 5:
            try:
                await bot.send_message(
                    TELEGRAM_GROUP_CHAT_ID,
                    "מי על הפיזורים והאיסופים של נועם ועמית היום?",
                    reply_markup=kb_morning(),
                )
            except Exception as exc:
                print(f"Logistics question error: {exc}", file=sys.stderr)

        # 4. Calendar summary (always — fallback to basic greeting)
        try:
            summary = _format_summary(events, today)
            await bot.send_message(TELEGRAM_GROUP_CHAT_ID, summary)
        except Exception as exc:
            print(f"Summary error: {exc}", file=sys.stderr)
            try:
                await bot.send_message(TELEGRAM_GROUP_CHAT_ID, "בוקר טוב! 🌅")
            except Exception:
                pass

    print("Morning job completed")


if __name__ == "__main__":
    asyncio.run(main())
