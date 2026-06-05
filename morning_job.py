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

CHILDREN_NAMES = {"נועם", "עמית"}

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
def _classify_event(ev: dict) -> tuple[bool, bool, set[str]]:
    """Return (is_ben, is_tal, children_covered_by_this_event)."""
    title = ev["title"]
    org   = ev["organizer_email"]
    is_ben = ("- בן" in title) or ("+ בן" in title) or (org in BEN_EMAILS)
    is_tal = ("- טל" in title) or ("+ טל" in title) or (org in TAL_EMAILS)
    covered = {name for name in CHILDREN_NAMES if name in title}
    return is_ben, is_tal, covered


def _check_child_coverage(events: list[dict]) -> list[str]:
    """Unified check: find any time window where a child has no available parent.

    Handles all cases:
      • Both parents solo busy         → all children need coverage
      • Parent A with Child X, Parent B solo
                                       → Child Y (the other child) needs coverage
      • Neutral child event (not yet attributed to a parent) overlapping a
        parent's busy slot             → that parent can't take the child
    """
    # ── attributed parent slots ────────────────────────────────────────────────
    ben_slots: list[tuple] = []   # (start, end, covered_children, title)
    tal_slots: list[tuple] = []
    neutral_child_events: list[tuple] = []   # (start, end, title)

    for ev in events:
        if ev["is_all_day"] or not ev["start"]:
            continue
        s = _ensure_aware(ev["start"])
        e = _ensure_aware(ev["end"]) if ev["end"] else s + timedelta(hours=1)
        is_ben, is_tal, covered = _classify_event(ev)
        if is_ben:
            ben_slots.append((s, e, covered, ev["title"]))
        if is_tal:
            tal_slots.append((s, e, covered, ev["title"]))
        # Neutral = involves a child but not attributed to either parent yet
        if any(name in ev["title"] for name in CHILDREN_NAMES) and not is_ben and not is_tal:
            neutral_child_events.append((s, e, ev["title"]))

    alerts: list[str] = []
    seen: set = set()

    # ── Case 1 & 2: overlapping parent slots ──────────────────────────────────
    for bs, be, b_cov, b_title in ben_slots:
        for ts, te, t_cov, t_title in tal_slots:
            overlap_s = max(bs, ts)
            overlap_e = min(be, te)
            if overlap_s >= overlap_e:
                continue

            all_covered   = b_cov | t_cov
            uncovered     = CHILDREN_NAMES - all_covered
            key = (overlap_s, frozenset(uncovered))
            if not uncovered or key in seen:
                continue
            seen.add(key)

            child_str = " ו".join(sorted(uncovered))
            time_str  = f"{overlap_s.strftime('%H:%M')}–{overlap_e.strftime('%H:%M')}"

            if not b_cov and not t_cov:
                # Both parents fully solo — classic overlap
                alerts.append(
                    f"🧒 {time_str}: שני ההורים עסוקים — מי שומר על {child_str}?"
                )
            else:
                # One parent is with a child, other is solo → sibling unattended
                alerts.append(
                    f"🧒 {time_str}: {child_str} ללא השגחה\n"
                    f"   בן: {b_title} | טל: {t_title}"
                )

    # ── Case 3: neutral child event overlaps a parent's busy slot ─────────────
    for cs, ce, ctitle in neutral_child_events:
        ben_busy = [t for bs, be, _, t in ben_slots if bs < ce and be > cs]
        tal_busy = [t for ts, te, _, t in tal_slots if ts < ce and te > cs]
        if ben_busy and tal_busy:
            alerts.append(
                f"⚠️ {ctitle}: שני ההורים עסוקים — "
                f"בן: {', '.join(ben_busy)} | טל: {', '.join(tal_busy)}"
            )
        elif ben_busy:
            alerts.append(f"⚠️ {ctitle}: בן עסוק ({', '.join(ben_busy)}) — טל מטפל/ת")
        elif tal_busy:
            alerts.append(f"⚠️ {ctitle}: טל עסוק/ה ({', '.join(tal_busy)}) — בן מטפל")

    return alerts


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
    # DST guard: both cron lines fire year-round (GitHub cron can't be conditional).
    # On a scheduled run, proceed only when the local Israel hour matches MORNING_HOUR
    # so exactly one of the two cron lines runs per day. Manual (workflow_dispatch) always runs.
    morning_hour = int(os.environ.get("MORNING_HOUR", "7"))
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        local_hour = datetime.now(TIMEZONE).hour
        if local_hour != morning_hour:
            print(f"Skipping: local hour {local_hour} != MORNING_HOUR {morning_hour} (wrong-season cron line)")
            return

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
        # 2. Unified child-coverage check
        try:
            coverage_alerts = _check_child_coverage(events) if events else []
            if coverage_alerts:
                print(f"Coverage alerts: {len(coverage_alerts)}")
                await bot.send_message(
                    TELEGRAM_GROUP_CHAT_ID,
                    "\n\n".join(coverage_alerts),
                )
        except Exception as exc:
            print(f"Child coverage check error: {exc}", file=sys.stderr)

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
