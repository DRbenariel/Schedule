"""CalDAV client wrapping the iCloud shared family calendar.

The `caldav` library is synchronous; all methods wrap blocking calls in `asyncio.to_thread()`.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import caldav
from caldav.elements import dav
from icalendar import Calendar as ICal
from icalendar import Event as ICalEvent
from icalendar import vDate

import config

logger = logging.getLogger(__name__)


@dataclass
class CalEvent:
    uid: str
    title: str
    start: datetime | date | None
    end: datetime | date | None
    is_all_day: bool


class CalDAVClient:
    """Thin async-friendly wrapper around the caldav library."""

    def __init__(self) -> None:
        self._client: caldav.DAVClient | None = None
        self._calendar: caldav.Calendar | None = None

    def _connect_sync(self) -> None:
        self._client = caldav.DAVClient(
            url="https://caldav.icloud.com/",
            username=config.ICLOUD_USER,
            password=config.ICLOUD_APP_PASSWORD,
        )
        principal = self._client.principal()
        target = config.ICLOUD_CALENDAR_NAME.strip()
        for cal in principal.calendars():
            try:
                props = cal.get_properties([dav.DisplayName()])
                name = props.get("{DAV:}displayname", "").strip()
            except Exception:
                name = (cal.name or "").strip()
            if name == target:
                self._calendar = cal
                logger.info("Connected to iCloud calendar %r", name)
                return
        available = [c.name for c in principal.calendars()]
        raise RuntimeError(f"Calendar {target!r} not found. Available: {available}")

    async def connect(self) -> None:
        await asyncio.to_thread(self._connect_sync)

    def _ensure_connected(self) -> caldav.Calendar:
        if self._calendar is None:
            raise RuntimeError("CalDAV client is not connected. Call connect() first.")
        return self._calendar

    # --------- read ---------

    def _get_events_sync(self, start: datetime, end: datetime) -> list[CalEvent]:
        cal = self._ensure_connected()
        results = cal.date_search(start=start, end=end, expand=True)
        events: list[CalEvent] = []
        for item in results:
            try:
                ical = ICal.from_ical(item.data)
            except Exception:
                logger.warning("Failed to parse event: %s", item.url)
                continue
            for component in ical.walk("VEVENT"):
                dtstart = component.get("dtstart")
                dtend = component.get("dtend")
                start_val = dtstart.dt if dtstart else None
                end_val = dtend.dt if dtend else None
                is_all_day = isinstance(start_val, date) and not isinstance(start_val, datetime)
                events.append(
                    CalEvent(
                        uid=str(component.get("uid", "")),
                        title=str(component.get("summary", "ללא כותרת")),
                        start=start_val,
                        end=end_val,
                        is_all_day=is_all_day,
                    )
                )
        return events

    async def get_events_for_range(self, start: datetime, end: datetime) -> list[CalEvent]:
        return await asyncio.to_thread(self._get_events_sync, start, end)

    async def check_conflicts(self, start: datetime, end: datetime) -> list[CalEvent]:
        events = await self.get_events_for_range(start, end)
        conflicts: list[CalEvent] = []
        for ev in events:
            if ev.start is None or ev.end is None:
                continue
            ev_start = _ensure_aware(ev.start)
            ev_end = _ensure_aware(ev.end)
            if ev_start < end and ev_end > start:
                conflicts.append(ev)
        return conflicts

    async def get_today_events(self) -> list[CalEvent]:
        now = datetime.now(config.TIMEZONE)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return await self.get_events_for_range(start, end)

    # --------- write ---------

    def _write_event_sync(
        self,
        title: str,
        start: datetime | None,
        end: datetime | None,
        is_all_day: bool,
        recurrence_rule: str | None,
        description: str | None = None,
    ) -> str:
        cal = self._ensure_connected()
        uid = str(uuid.uuid4())
        ical = ICal()
        ical.add("prodid", "-//FamilyCalBot//HE//")
        ical.add("version", "2.0")
        event = ICalEvent()
        event.add("uid", uid)
        event.add("summary", title)
        event.add("dtstamp", datetime.now(config.TIMEZONE))

        if is_all_day:
            day = start.date() if isinstance(start, datetime) else (start or datetime.now(config.TIMEZONE).date())
            event.add("dtstart", vDate(day))
            event.add("dtend", vDate(day + timedelta(days=1)))
        else:
            assert start is not None
            event.add("dtstart", start)
            event.add("dtend", end or (start + timedelta(hours=1)))

        if recurrence_rule:
            rrule_dict = _parse_rrule(recurrence_rule)
            if rrule_dict:
                event.add("rrule", rrule_dict)

        if description:
            event.add("description", description)

        ical.add_component(event)
        cal.add_event(ical.to_ical().decode("utf-8"))
        return uid

    async def write_event(
        self,
        title: str,
        start: datetime,
        end: datetime | None = None,
        recurrence_rule: str | None = None,
        description: str | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self._write_event_sync, title, start, end, False, recurrence_rule, description
        )

    async def write_all_day(self, title: str, day: date, description: str | None = None) -> str:
        return await asyncio.to_thread(
            self._write_event_sync, title, datetime.combine(day, datetime.min.time()), None, True, None, description
        )

    # --------- update ---------

    def _update_title_sync(self, uid: str, new_title: str) -> bool:
        cal = self._ensure_connected()
        try:
            event = cal.event_by_uid(uid)
        except Exception:
            logger.warning("Event UID %s not found in calendar", uid)
            return False
        try:
            ical = ICal.from_ical(event.data)
            for component in ical.walk("VEVENT"):
                if str(component.get("uid", "")) == uid:
                    if "summary" in component:
                        del component["summary"]
                    component.add("summary", new_title)
            event.data = ical.to_ical().decode("utf-8")
            event.save()
            return True
        except Exception:
            logger.exception("Failed to update title for UID %s", uid)
            return False

    async def update_event_title(self, uid: str, new_title: str) -> bool:
        return await asyncio.to_thread(self._update_title_sync, uid, new_title)

    def _update_description_sync(self, uid: str, description: str) -> bool:
        cal = self._ensure_connected()
        try:
            event = cal.event_by_uid(uid)
        except Exception:
            logger.warning("Event UID %s not found for description update", uid)
            return False
        try:
            ical = ICal.from_ical(event.data)
            for component in ical.walk("VEVENT"):
                if str(component.get("uid", "")) == uid:
                    if "description" in component:
                        del component["description"]
                    component.add("description", description)
            event.data = ical.to_ical().decode("utf-8")
            event.save()
            return True
        except Exception:
            logger.exception("Failed to update description for UID %s", uid)
            return False

    async def update_event_description(self, uid: str, description: str) -> bool:
        return await asyncio.to_thread(self._update_description_sync, uid, description)


# --------- helpers ---------

def _ensure_aware(value: datetime | date) -> datetime:
    """Normalise a date or naive datetime into a tz-aware datetime in the project timezone."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return config.TIMEZONE.localize(value)
        return value
    # Plain date (all-day event) — treat as midnight at start of that day.
    return config.TIMEZONE.localize(datetime.combine(value, datetime.min.time()))


def _parse_rrule(rrule_string: str) -> dict[str, list[str] | str] | None:
    """Parse 'FREQ=WEEKLY;BYDAY=SU' into the dict shape that icalendar expects."""
    try:
        out: dict[str, list[str] | str] = {}
        for part in rrule_string.split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip().upper()
            value = value.strip()
            if "," in value:
                out[key] = [v.strip() for v in value.split(",")]
            else:
                out[key] = value
        return out or None
    except Exception:
        logger.exception("Failed to parse RRULE %r", rrule_string)
        return None


HEB_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]


def format_event_line(ev: CalEvent) -> str:
    if ev.is_all_day:
        return f"📌 {ev.title} (כל היום)"
    start = _ensure_aware(ev.start) if ev.start else None
    end = _ensure_aware(ev.end) if ev.end else None
    if start and end:
        return f"🕒 {start.strftime('%H:%M')}–{end.strftime('%H:%M')} | {ev.title}"
    if start:
        return f"🕒 {start.strftime('%H:%M')} | {ev.title}"
    return f"• {ev.title}"


def format_day_summary(events: list[CalEvent], day: datetime) -> str:
    weekday = HEB_DAYS[day.weekday()]
    header = f"📅 {weekday}, {day.strftime('%d/%m/%Y')}"
    if not events:
        return f"{header}\n\n אין אירועים מתוכננים להיום."
    sorted_events = sorted(
        events,
        key=lambda e: (_ensure_aware(e.start) if e.start else datetime.max.replace(tzinfo=config.TIMEZONE)),
    )
    lines = [header, ""] + [format_event_line(e) for e in sorted_events]
    return "\n".join(lines)
