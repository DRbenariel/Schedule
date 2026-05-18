"""Telegram bot entry point: handlers, scheduler, webhook server.

All UI text is Hebrew. Conversation state is persisted to SQLite so a Cloud Run cold
restart doesn't lose in-flight confirmations.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import db
from calendar_client import CalDAVClient, format_day_summary
from nlp import ParsedEvent, ParseError, parse_event_text, parse_event_with_context

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------- conversation state ----------
STATE_AWAITING_ASSIGN = "AWAITING_ASSIGN"
STATE_AWAITING_CONFIRM = "AWAITING_CONFIRM"
STATE_AWAITING_FORCE = "AWAITING_FORCE"
STATE_AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"

# ---------- assignment choices ----------
ASSIGN_TAL = "טל"
ASSIGN_BEN = "בן"
ASSIGN_BOTH = "שניהם"
ASSIGN_LATER = "later"
ASSIGN_OTHER = "other"

# ---------- callback prefixes ----------
CB_ASSIGN = "a:"        # a:טל / a:בן / a:שניהם / a:later / a:other / a:ext:<name>
CB_CONFIRM = "c:"       # c:yes / c:no
CB_FORCE = "f:"         # f:yes / f:no
CB_MORNING = "m:"       # m:0 .. m:3
CB_PENDING = "pa:"      # pa:<row_id>:<choice>  (post-creation pending assignment)
CB_CHILDCARE = "ch:"    # ch:<row_id>:<choice>  (who's watching the kids after שניהם)
CB_EXT_CC = "ce:"       # ce:<row_id>:<name>    (extended family childcare answer)

# ---------- morning options ----------
MORNING_OPTIONS = [
    "טל מפזר/ת, בן אוסף/ת",
    "בן מפזר/ת, טל אוסף/ת",
    "טל גם מפזר/ת וגם אוסף/ת",
    "בן גם מפזר/ת וגם אוסף/ת",
]


# ===================================================================
# Keyboards
# ===================================================================
def kb_assign_select(involves_children: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("טל", callback_data=f"{CB_ASSIGN}{ASSIGN_TAL}"),
            InlineKeyboardButton("בן", callback_data=f"{CB_ASSIGN}{ASSIGN_BEN}"),
        ],
        [
            InlineKeyboardButton("שניהם", callback_data=f"{CB_ASSIGN}{ASSIGN_BOTH}"),
            InlineKeyboardButton("להחליט מאוחר", callback_data=f"{CB_ASSIGN}{ASSIGN_LATER}"),
        ],
    ]
    if involves_children:
        rows.append([InlineKeyboardButton("אחר", callback_data=f"{CB_ASSIGN}{ASSIGN_OTHER}")])
    return InlineKeyboardMarkup(rows)


def kb_ext_assign() -> InlineKeyboardMarkup:
    """Extended family members for initial assignment flow."""
    names = config.EXTENDED_FAMILY
    rows = []
    for i in range(0, len(names), 3):
        row = [InlineKeyboardButton(n, callback_data=f"{CB_ASSIGN}ext:{n}") for n in names[i:i+3]]
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def kb_childcare(row_id: int) -> InlineKeyboardMarkup:
    """Who's watching the kids? (after שניהם — only extended family + defer)."""
    names = config.EXTENDED_FAMILY
    rows = []
    for i in range(0, len(names), 3):
        row = [InlineKeyboardButton(n, callback_data=f"{CB_EXT_CC}{row_id}:{n}") for n in names[i:i+3]]
        rows.append(row)
    rows.append([InlineKeyboardButton("להחליט מאוחר", callback_data=f"{CB_CHILDCARE}{row_id}:later")])
    return InlineKeyboardMarkup(rows)


def kb_pending_select(row_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("טל", callback_data=f"{CB_PENDING}{row_id}:{ASSIGN_TAL}"),
            InlineKeyboardButton("בן", callback_data=f"{CB_PENDING}{row_id}:{ASSIGN_BEN}"),
        ],
        [
            InlineKeyboardButton("שניהם", callback_data=f"{CB_PENDING}{row_id}:{ASSIGN_BOTH}"),
            InlineKeyboardButton("אחר כך", callback_data=f"{CB_PENDING}{row_id}:{ASSIGN_LATER}"),
        ],
    ])


def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ אשר", callback_data=f"{CB_CONFIRM}yes"),
            InlineKeyboardButton("❌ בטל", callback_data=f"{CB_CONFIRM}no"),
        ]]
    )


def kb_force() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("⚠️ שבץ בכל זאת", callback_data=f"{CB_FORCE}yes"),
            InlineKeyboardButton("❌ בטל", callback_data=f"{CB_FORCE}no"),
        ]]
    )


def kb_morning() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(opt, callback_data=f"{CB_MORNING}{i}")] for i, opt in enumerate(MORNING_OPTIONS)]
    return InlineKeyboardMarkup(rows)


# ===================================================================
# State helpers
# ===================================================================
def _resolve_parent(telegram_id: int) -> str | None:
    return config.PARENT_MAP.get(telegram_id)


def _state_is_fresh(updated_at: str) -> bool:
    try:
        ts = datetime.fromisoformat(updated_at)
    except Exception:
        return False
    return datetime.utcnow() - ts < timedelta(minutes=config.STATE_TIMEOUT_MINUTES)


def _format_event_for_confirm(parsed: ParsedEvent) -> str:
    if parsed.intent == "task":
        return f"משימה: '{parsed.title}'"
    if parsed.start_time:
        date_str = parsed.start_time.strftime("%d/%m/%Y")
        time_str = parsed.start_time.strftime("%H:%M")
        return f"'{parsed.title}' ב-{date_str} בשעה {time_str}"
    return f"'{parsed.title}'"


# ===================================================================
# Confirmation flow helpers
# ===================================================================
def _payload_with_pending(parsed: ParsedEvent, pending: bool) -> dict:
    payload = parsed.to_dict()
    payload["_pending_assignment"] = pending
    return payload


def _build_title_with_assignment(
    base_title: str,
    involves_children: list[str],
    assignment: str,
) -> str:
    """Return the canonical CalDAV title: 'event - child + parent' or 'event - parent'.

    assignment should be one of ASSIGN_TAL, ASSIGN_BEN, ASSIGN_BOTH, ASSIGN_LATER,
    or an extended-family name.
    """
    # Don't double-suffix if already assigned
    for marker in ("- טל", "- בן", "+ טל", "+ בן", "+ להחליט"):
        if marker in base_title:
            return base_title

    child_str = " ו".join(involves_children) if involves_children else ""

    if assignment == ASSIGN_BOTH:
        suffix = "בן וטל"
    elif assignment == ASSIGN_LATER:
        suffix = "להחליט" if child_str else None
    else:
        suffix = assignment  # "טל", "בן", or extended-family name

    if not suffix:
        return base_title
    if child_str:
        return f"{base_title} - {child_str} + {suffix}"
    return f"{base_title} - {suffix}"


def _filter_conflicts_for_parent(
    conflicts: list, assigned_parent: str | None
) -> list:
    """Remove conflicts that belong exclusively to the *other* parent.

    This prevents Ben's event from being blocked by Tal's simultaneous event.
    Events with no parent suffix (shared/family events) are always kept.
    """
    if not assigned_parent:
        return conflicts
    other = ASSIGN_BEN if assigned_parent == ASSIGN_TAL else ASSIGN_TAL

    def _is_other_only(title: str) -> bool:
        has_other = (f"- {other}" in title or f"+ {other}" in title)
        has_mine  = (f"- {assigned_parent}" in title or f"+ {assigned_parent}" in title)
        return has_other and not has_mine

    return [c for c in conflicts if not _is_other_only(c.title)]


async def _proceed_after_parsing(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parsed: ParsedEvent,
    pending: bool = False,
    childcare_needed: bool = False,
) -> None:
    """After parsing (and optional assignment selection), check conflicts and prompt."""
    chat_id = update.effective_chat.id
    conn: "db.sqlite3.Connection" = context.application.bot_data["db"]
    caldav: CalDAVClient = context.application.bot_data["caldav"]

    def _make_payload() -> dict:
        p = _payload_with_pending(parsed, pending)
        p["_childcare_needed"] = childcare_needed
        return p

    if parsed.intent == "task" or parsed.start_time is None:
        # Tasks become all-day events. Skip conflict check.
        db.save_state(conn, chat_id, STATE_AWAITING_CONFIRM, _make_payload())
        await update.effective_chat.send_message(
            f"הבנתי: {_format_event_for_confirm(parsed)}. לשבץ ביומן?",
            reply_markup=kb_confirm(),
        )
        return

    end = parsed.end_time or (parsed.start_time + timedelta(hours=1))
    try:
        conflicts = await caldav.check_conflicts(parsed.start_time, end)
    except Exception:
        logger.exception("Conflict check failed")
        conflicts = []

    # Filter out conflicts that belong to the *other* parent only.
    # (e.g. Ben taking a child to swim should not be blocked by Tal's night shift)
    assigned_parent = None
    for p in (ASSIGN_TAL, ASSIGN_BEN):
        if f"- {p}" in parsed.title or f"+ {p}" in parsed.title:
            assigned_parent = p
            break
    conflicts = _filter_conflicts_for_parent(conflicts, assigned_parent)

    if conflicts:
        names = ", ".join(c.title for c in conflicts[:3])
        db.save_state(conn, chat_id, STATE_AWAITING_FORCE, _make_payload())
        await update.effective_chat.send_message(
            f"⚠️ שימו לב, יש התנגשות עם: {names}.\nלשבץ בכל זאת?",
            reply_markup=kb_force(),
        )
    else:
        db.save_state(conn, chat_id, STATE_AWAITING_CONFIRM, _make_payload())
        await update.effective_chat.send_message(
            f"הבנתי: {_format_event_for_confirm(parsed)}. לשבץ ביומן?",
            reply_markup=kb_confirm(),
        )


async def _commit_event(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parsed: ParsedEvent,
    pending_assignment: bool = False,
    childcare_needed: bool = False,
) -> None:
    """Write the event to CalDAV. If `pending_assignment` is True, also insert
    a row into `pending_assignment` so the morning trigger asks who's taking it.
    If `childcare_needed` is True, ask who's watching the kids after writing.
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    conn = context.application.bot_data["db"]
    caldav: CalDAVClient = context.application.bot_data["caldav"]

    try:
        if parsed.intent == "task" or parsed.start_time is None:
            day = (parsed.start_time or datetime.now(config.TIMEZONE)).date()
            uid = await caldav.write_all_day(parsed.title, day)
            db.log_caldav_write(conn, uid, parsed.title, None, None, True, user_id)
            event_date = day.isoformat()
        else:
            end = parsed.end_time or (parsed.start_time + timedelta(hours=1))
            uid = await caldav.write_event(
                parsed.title, parsed.start_time, end, parsed.recurrence_rule
            )
            db.log_caldav_write(
                conn, uid, parsed.title,
                parsed.start_time.isoformat(), end.isoformat(),
                False, user_id,
            )
            event_date = parsed.start_time.date().isoformat()
    except Exception:
        logger.exception("CalDAV write failed")
        await update.effective_chat.send_message("❌ שגיאה בכתיבה ליומן. נסו שוב מאוחר יותר.")
        db.clear_state(conn, chat_id)
        return

    if pending_assignment:
        db.add_pending_assignment(conn, uid, parsed.title, event_date, chat_id)

    db.clear_state(conn, chat_id)
    suffix = " (יישאל בבוקר מי לוקח)" if pending_assignment else ""
    await update.effective_chat.send_message(
        f"✅ שובץ ביומן: {_format_event_for_confirm(parsed)}{suffix}"
    )

    # Fix 2: after writing a "שניהם" event, ask who's watching the kids
    if childcare_needed:
        childcare_row_id = db.add_pending_assignment(
            conn, uid, f"שמירה: {parsed.title}", event_date, chat_id
        )
        await update.effective_chat.send_message(
            "מי שומר על נועם ועמית?",
            reply_markup=kb_childcare(childcare_row_id),
        )


# ===================================================================
# Handlers
# ===================================================================
async def cmd_cron_morning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Triggered by GitHub Actions at 7am. Validates secret then runs morning routine."""
    args = context.args or []
    secret = args[0] if args else ""
    if config.CRON_SECRET and secret != config.CRON_SECRET:
        logger.warning("Unauthorized /cron_morning attempt")
        return
    try:
        await update.message.delete()
    except Exception:
        pass
    await morning_routine(context.application)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "שלום! אני בוט לוח השנה המשפחתי.\n"
        "שלחו לי הודעה חופשית בעברית (למשל: 'תור לרופא לעמית ביום שלישי ב-10:00')\n"
        "ואני אשבץ ביומן Apple המשותף לאחר אישור."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "פקודות זמינות:\n"
        "/today — אירועי היום\n"
        "/help — עזרה\n\n"
        "כדי לקבוע אירוע, פשוט כתבו אותו במילים שלכם."
    )


async def cmd_testmorning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: manually trigger the morning routine right now, with step-by-step logging."""
    chat_id = update.effective_chat.id
    app = context.application
    conn = app.bot_data["db"]
    caldav: CalDAVClient = app.bot_data["caldav"]
    today = datetime.now(config.TIMEZONE).date()

    await update.message.reply_text("🔧 שלב 1: בודק pending assignments…")
    try:
        class _Ctx:
            bot = app.bot
            bot_data = app.bot_data
            application = app
        await asyncio.wait_for(
            _ask_pending_for_date(_Ctx(), chat_id, today.isoformat(), day_before=False), timeout=10.0
        )
        await update.message.reply_text("✅ שלב 1 הושלם.")
    except Exception as e:
        await update.message.reply_text(f"❌ שלב 1 נכשל: {e}")

    await update.message.reply_text("🔧 שלב 2: בודק חפיפה בין הורים ביומן…")
    try:
        day_start = _to_aware(today)
        day_end = day_start + timedelta(days=1)
        events = await asyncio.wait_for(
            caldav.get_events_for_range(day_start, day_end), timeout=25.0
        )
        timed = [e for e in events if not e.is_all_day and e.start]
        ben_events = [e.title for e in timed if "- בן" in e.title]
        tal_events = [e.title for e in timed if "- טל" in e.title]
        all_titles = [f"• {e.title}" for e in timed]
        debug = (
            f"📅 אירועים היום ({len(timed)} ממוזמנים):\n" +
            ("\n".join(all_titles) if all_titles else "אין") +
            f"\n\nבן: {ben_events or 'אין'}\nטל: {tal_events or 'אין'}"
        )
        await update.message.reply_text(debug)
        await asyncio.wait_for(_check_parents_overlap(app, chat_id, today), timeout=25.0)
        await update.message.reply_text("✅ שלב 2 הושלם.")
    except asyncio.TimeoutError:
        await update.message.reply_text("⏰ שלב 2 timeout — בעיה בחיבור iCloud.")
    except Exception as e:
        await update.message.reply_text(f"❌ שלב 2 נכשל: {e}")

    await update.message.reply_text("🔧 שלב 3: שולח הודעת בוקר…")
    try:
        await app.bot.send_message(
            config.TELEGRAM_GROUP_CHAT_ID,
            "בוקר טוב! 🌅\nמי על הפיזורים והאיסופים של נועם ועמית היום?",
            reply_markup=kb_morning(),
        )
        await update.message.reply_text("✅ שלב 3 הושלם — רוטינת הבוקר עבדה!")
    except Exception as e:
        await update.message.reply_text(f"❌ שלב 3 נכשל: {e}")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caldav: CalDAVClient = context.application.bot_data["caldav"]
    try:
        events = await caldav.get_today_events()
    except Exception:
        logger.exception("Failed to fetch today's events")
        await update.message.reply_text("❌ לא הצלחתי לטעון את אירועי היום.")
        return
    summary = format_day_summary(events, datetime.now(config.TIMEZONE))
    await update.message.reply_text(summary)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    sender_name = _resolve_parent(user.id) if user else None
    if sender_name is None:
        await update.message.reply_text("⚠️ המשתמש שלכם לא ממופה להורה. בדקו TAL_TELEGRAM_ID / BEN_TELEGRAM_ID.")
        return

    # "בוקר טוב" → trigger morning routine
    if text.strip() in ("בוקר טוב", "בוקר טוב!"):
        asyncio.create_task(morning_routine(context.application))
        return

    await update.effective_chat.send_action("typing")
    conn = context.application.bot_data["db"]

    # ---- Fix 1: Check if we're in a clarification Q&A loop ----
    state = db.load_state(conn, chat_id)
    if state and state["state_name"] == STATE_AWAITING_CLARIFICATION and _state_is_fresh(state["updated_at"]):
        payload = state["payload"]
        original_text = payload["original_text"]
        history: list = payload.get("history", [])
        last_q = payload.get("last_question", "")
        history = history + [(last_q, text)]  # append latest answer

        try:
            parsed = await parse_event_with_context(original_text, history, sender_name)
        except ParseError:
            await update.message.reply_text("❌ לא הצלחתי להבין. נסחו מחדש מההתחלה.")
            db.clear_state(conn, chat_id)
            return
        except Exception:
            logger.exception("Unexpected NLP error (context mode)")
            await update.message.reply_text("❌ שגיאה בעיבוד ההודעה.")
            return

        if parsed.needs_clarification:
            # Still unclear — save updated history and ask again
            db.save_state(conn, chat_id, STATE_AWAITING_CLARIFICATION, {
                "original_text": original_text,
                "history": history,
                "last_question": parsed.clarification_needed,
            })
            await update.message.reply_text(parsed.clarification_needed)
            return

        db.clear_state(conn, chat_id)
        # Continue to assignment / confirm flow with the now-resolved event
        if not parsed.mentioned_parents:
            db.save_state(conn, chat_id, STATE_AWAITING_ASSIGN, parsed.to_dict())
            if parsed.involves_children:
                kid_str = " ו".join(parsed.involves_children)
                prompt = f"זה אירוע של {kid_str}. מי לוקח?"
            else:
                prompt = "למי לשייך את האירוע?"
            await update.message.reply_text(prompt, reply_markup=kb_assign_select(bool(parsed.involves_children)))
            return
        both_parents = set(parsed.mentioned_parents) >= {ASSIGN_TAL, ASSIGN_BEN}
        assignment = ASSIGN_BOTH if both_parents else (list(parsed.mentioned_parents)[0] if parsed.mentioned_parents else None)
        if assignment:
            parsed.title = _build_title_with_assignment(parsed.title, parsed.involves_children, assignment)
        await _proceed_after_parsing(update, context, parsed, childcare_needed=both_parents)
        return

    # ---- Normal flow (no clarification state) ----
    try:
        parsed = await parse_event_text(text, sender_name)
    except ParseError:
        logger.exception("Parse error")
        await update.message.reply_text("❌ לא הצלחתי להבין את ההודעה. נסחו שוב בבקשה.")
        return
    except Exception:
        logger.exception("Unexpected NLP error")
        await update.message.reply_text("❌ שגיאה בעיבוד ההודעה.")
        return

    if parsed.needs_clarification:
        # Save clarification state so the next reply has context
        db.save_state(conn, chat_id, STATE_AWAITING_CLARIFICATION, {
            "original_text": text,
            "history": [],
            "last_question": parsed.clarification_needed,
        })
        await update.message.reply_text(parsed.clarification_needed or "תוכלו להבהיר את התאריך/שעה?")
        return

    # If a parent name is explicitly mentioned in the text, skip the assignment prompt.
    # Otherwise (covers child events AND adult/family events alike) → ask.
    if not parsed.mentioned_parents:
        db.save_state(conn, chat_id, STATE_AWAITING_ASSIGN, parsed.to_dict())
        if parsed.involves_children:
            kid_str = " ו".join(parsed.involves_children)
            prompt = f"זה אירוע של {kid_str}. מי לוקח?"
        else:
            prompt = "למי לשייך את האירוע?"
        await update.message.reply_text(prompt, reply_markup=kb_assign_select(bool(parsed.involves_children)))
        return

    both_parents = set(parsed.mentioned_parents) >= {ASSIGN_TAL, ASSIGN_BEN}
    assignment = ASSIGN_BOTH if both_parents else (list(parsed.mentioned_parents)[0] if parsed.mentioned_parents else None)
    if assignment:
        parsed.title = _build_title_with_assignment(parsed.title, parsed.involves_children, assignment)
    await _proceed_after_parsing(update, context, parsed, childcare_needed=both_parents)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    chat_id = update.effective_chat.id
    conn = context.application.bot_data["db"]

    # ---------- morning answers ----------
    if data.startswith(CB_MORNING):
        idx = int(data[len(CB_MORNING):])
        if 0 <= idx < len(MORNING_OPTIONS):
            answer = MORNING_OPTIONS[idx]
            today = datetime.now(config.TIMEZONE).date()
            today_str = today.isoformat()
            # Check if already answered today — avoid duplicate CalDAV writes
            existing = db.get_morning_answer(conn, today_str)
            db.save_morning_answer(conn, today_str, answer, update.effective_user.id if update.effective_user else 0)
            if not existing:
                try:
                    caldav: CalDAVClient = context.application.bot_data["caldav"]
                    await caldav.write_all_day(f"לוגיסטיקה: {answer}", today)
                except Exception:
                    logger.exception("Logistics CalDAV write failed (non-fatal)")
            await query.edit_message_text(f"✅ נרשם: {answer}")
            await _send_daily_summary(context, chat_id)
        return

    # ---------- pending-assignment answers (post-creation, asked at morning trigger) ----------
    if data.startswith(CB_PENDING):
        await _handle_pending_callback(update, context, query, data)
        return

    # ---------- childcare callbacks — no active state needed ----------
    if data.startswith(CB_EXT_CC):
        body = data[len(CB_EXT_CC):]
        try:
            row_id_str, caregiver = body.split(":", 1)
            row_id = int(row_id_str)
        except (ValueError, IndexError):
            return
        row = db.get_pending_by_id(conn, row_id)
        if not row or row["resolved"]:
            await query.edit_message_text("⏰ הפעולה כבר נסגרה.")
            return
        db.resolve_pending(conn, row_id, caregiver)
        uid = row.get("event_uid", "")
        if uid and not uid.startswith("childcare-"):
            caldav: CalDAVClient = context.application.bot_data["caldav"]
            await caldav.update_event_description(uid, f"בייביסיטר {caregiver}")
        await query.edit_message_text(f"✅ {caregiver} ישמור על הילדים.")
        return

    if data.startswith(CB_CHILDCARE):
        body = data[len(CB_CHILDCARE):]
        try:
            row_id_str, choice = body.split(":", 1)
            row_id = int(row_id_str)
        except (ValueError, IndexError):
            return
        row = db.get_pending_by_id(conn, row_id)
        if not row or row["resolved"]:
            await query.edit_message_text("⏰ הפעולה כבר נסגרה.")
            return
        if choice == "later":
            await query.edit_message_text("בסדר, תישאלו שוב ביום האירוע.")
        return

    # All other callbacks require a stored state.
    state = db.load_state(conn, chat_id)
    if not state or not _state_is_fresh(state["updated_at"]):
        await query.edit_message_text("⏰ הפעולה פגה. שלחו את ההודעה שוב.")
        db.clear_state(conn, chat_id)
        return

    parsed = ParsedEvent.from_dict(state["payload"])
    pending_flag = bool(state["payload"].get("_pending_assignment", False))
    childcare_flag = bool(state["payload"].get("_childcare_needed", False))

    # ---------- assignment selection ----------
    if data.startswith(CB_ASSIGN):
        choice = data[len(CB_ASSIGN):]
        pending = False

        if choice == ASSIGN_TAL or choice == ASSIGN_BEN:
            parsed.title = _build_title_with_assignment(parsed.title, parsed.involves_children, choice)
            await query.edit_message_text(f"שויך ל-{choice}.")

        elif choice == ASSIGN_BOTH:
            parsed.title = _build_title_with_assignment(parsed.title, parsed.involves_children, ASSIGN_BOTH)
            await query.edit_message_text("שניהם. ממשיך לאישור.")
            await _proceed_after_parsing(update, context, parsed, pending=False, childcare_needed=True)
            return

        elif choice == ASSIGN_LATER:
            pending = True
            parsed.title = _build_title_with_assignment(parsed.title, parsed.involves_children, ASSIGN_LATER)
            await query.edit_message_text("נשמור ונשאל בבוקר.")

        elif choice == ASSIGN_OTHER:
            await query.edit_message_reply_markup(reply_markup=kb_ext_assign())
            return

        elif choice.startswith("ext:"):
            caregiver = choice[4:]
            parsed.title = _build_title_with_assignment(parsed.title, parsed.involves_children, caregiver)
            await query.edit_message_text(f"שויך ל-{caregiver}.")

        else:
            return

        # Continue to conflict check / confirm. Carry the pending flag through state.
        payload = parsed.to_dict()
        payload["_pending_assignment"] = pending
        db.save_state(conn, chat_id, STATE_AWAITING_CONFIRM, payload)
        await _proceed_after_parsing(update, context, parsed, pending=pending)
        return

    # ---------- confirm ----------
    if data.startswith(CB_CONFIRM):
        if data.endswith("yes"):
            await query.edit_message_text("מעדכן יומן…")
            await _commit_event(update, context, parsed, pending_assignment=pending_flag, childcare_needed=childcare_flag)
        else:
            db.clear_state(conn, chat_id)
            await query.edit_message_text("❌ בוטל.")
        return

    # ---------- force on conflict ----------
    if data.startswith(CB_FORCE):
        if data.endswith("yes"):
            await query.edit_message_text("מעדכן יומן למרות ההתנגשות…")
            await _commit_event(update, context, parsed, pending_assignment=pending_flag, childcare_needed=childcare_flag)
        else:
            db.clear_state(conn, chat_id)
            await query.edit_message_text("❌ בוטל.")
        return


# ===================================================================
# Daily summary + Morning routine
# ===================================================================
async def _handle_pending_callback(update, context, query, data: str) -> None:
    """Handle the morning's pending-assignment buttons. data = 'pa:<row_id>:<choice>'."""
    conn = context.application.bot_data["db"]
    caldav: CalDAVClient = context.application.bot_data["caldav"]
    body = data[len(CB_PENDING):]
    try:
        row_id_str, choice = body.split(":", 1)
        row_id = int(row_id_str)
    except (ValueError, IndexError):
        return

    row = db.get_pending_by_id(conn, row_id)
    if not row or row["resolved"]:
        await query.edit_message_text("⏰ הפעולה כבר נסגרה.")
        return

    if choice == ASSIGN_LATER:
        await query.edit_message_text(f"בסדר, נשאל שוב מאוחר יותר לגבי '{row['title']}'.")
        return

    if choice in (ASSIGN_TAL, ASSIGN_BEN):
        base = row['title']
        # If the pending placeholder was written (child event: "X - נועם + להחליט"),
        # replace the placeholder rather than appending again.
        if '+ להחליט' in base:
            new_title = base.replace('+ להחליט', f'+ {choice}')
        else:
            new_title = f"{base} - {choice}"
        ok = await caldav.update_event_title(row["event_uid"], new_title)
        if not ok:
            await query.edit_message_text("❌ לא הצלחתי לעדכן את היומן.")
            return
        db.resolve_pending(conn, row_id, choice)
        await query.edit_message_text(f"✅ '{row['title']}' שויך ל-{choice}.")
        return

    if choice == ASSIGN_BOTH:
        base = row['title']
        if '+ להחליט' in base:
            new_title = base.replace('+ להחליט', '+ בן וטל')
            await caldav.update_event_title(row["event_uid"], new_title)
        db.resolve_pending(conn, row_id, ASSIGN_BOTH)
        await query.edit_message_text(f"✅ '{row['title']}' נותר משותף.")
        return


def _to_aware(dt: "datetime | date") -> datetime:
    """Ensure a datetime or date is timezone-aware in the project timezone."""
    from datetime import date as date_type
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else config.TIMEZONE.localize(dt)
    return config.TIMEZONE.localize(datetime.combine(dt, datetime.min.time()))


async def _check_parents_overlap(
    application: Application, chat_id: int, today: "date"
) -> None:
    """Detect overlapping parent events today and ask childcare question if needed."""
    from datetime import date as date_type
    caldav: CalDAVClient = application.bot_data["caldav"]
    conn = application.bot_data["db"]

    day_start = _to_aware(today)
    day_end = day_start + timedelta(days=1)

    try:
        events = await asyncio.wait_for(
            caldav.get_events_for_range(day_start, day_end), timeout=20.0
        )
    except asyncio.TimeoutError:
        logger.warning("CalDAV timeout during overlap check — skipping")
        return
    except Exception:
        logger.exception("Failed to fetch events for overlap check")
        return

    # Parent email sets for organizer-based detection
    _BEN_EMAILS = {"benariel@gmail.com", "benariel@icloud.com"}
    _TAL_EMAILS = {"tal8202@gmail.com", "tal8202@icloud.com"}

    # Skip all-day events; collect timed events per parent
    ben_slots, tal_slots = [], []
    for ev in events:
        if ev.is_all_day or not ev.start:
            continue
        s = _to_aware(ev.start)
        e = _to_aware(ev.end) if ev.end else s + timedelta(hours=1)
        is_ben = ("- בן" in ev.title or "- בן וטל" in ev.title
                  or ev.organizer_email in _BEN_EMAILS)
        is_tal = ("- טל" in ev.title or "- בן וטל" in ev.title
                  or ev.organizer_email in _TAL_EMAILS)
        if is_ben:
            ben_slots.append((s, e))
        if is_tal:
            tal_slots.append((s, e))

    # Check for any overlap between בן and טל
    overlap_found = any(
        bs < te and be > ts
        for bs, be in ben_slots
        for ts, te in tal_slots
    )
    if not overlap_found:
        return

    # Avoid asking twice on the same day
    existing = db.get_pending_for_date(conn, today.isoformat(), day_before=False)
    if any("שמירה:" in r["title"] for r in existing):
        return

    # Create a pending_assignment row so the answer is tracked
    row_id = db.add_pending_assignment(
        conn,
        f"childcare-{today.isoformat()}",
        f"שמירה: שני ההורים עסוקים",
        today.isoformat(),
        chat_id,
    )
    await application.bot.send_message(
        chat_id,
        "🧒 שני ההורים עסוקים בו-זמנית היום — מי שומר על נועם ועמית?",
        reply_markup=kb_childcare(row_id),
    )


async def _ask_pending_for_date(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, target_date: str, day_before: bool
) -> None:
    conn = context.application.bot_data["db"]
    rows = db.get_pending_for_date(conn, target_date, day_before=day_before)
    for row in rows:
        when = "מחר" if day_before else "היום"
        # Childcare rows use a different keyboard and wording
        if row["title"].startswith("שמירה:"):
            event_desc = row["title"][6:].strip()
            await context.bot.send_message(
                chat_id,
                f"🧒 {when}: {event_desc} — מי שומר על נועם ועמית?",
                reply_markup=kb_childcare(row["id"]),
            )
        else:
            await context.bot.send_message(
                chat_id,
                f"❓ {when}: '{row['title']}' — מי לוקח?",
                reply_markup=kb_pending_select(row["id"]),
            )
        if day_before:
            db.mark_asked_day_before(conn, row["id"])


async def _send_daily_summary(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    caldav: CalDAVClient = context.application.bot_data["caldav"]
    try:
        events = await asyncio.wait_for(caldav.get_today_events(), timeout=20.0)
    except asyncio.TimeoutError:
        logger.warning("CalDAV timeout fetching daily summary")
        await context.bot.send_message(chat_id, "⚠️ לא הצלחתי לטעון את אירועי היום (timeout).")
        return
    except Exception:
        logger.exception("Failed to fetch events for daily summary")
        return
    summary = format_day_summary(events, datetime.now(config.TIMEZONE))
    await context.bot.send_message(chat_id, summary)


async def _check_child_coverage_and_notify(
    application: Application, chat_id: int, today: "date"
) -> None:
    """Fetch today's events and send child-coverage alerts (unified logic)."""
    _BEN_EMAILS = {"benariel@gmail.com", "benariel@icloud.com"}
    _TAL_EMAILS = {"tal8202@gmail.com", "tal8202@icloud.com"}
    _CHILDREN   = {"נועם", "עמית"}
    conn   = application.bot_data["db"]
    caldav = application.bot_data["caldav"]

    day_start = _to_aware(today)
    day_end   = day_start + timedelta(days=1)
    try:
        raw_events = await asyncio.wait_for(
            caldav.get_events_for_range(day_start, day_end), timeout=20.0
        )
    except Exception:
        logger.exception("Failed to fetch events for child-coverage check — skipping")
        return

    # Convert CalEvent objects to the dict shape used by the coverage logic
    events = [
        {
            "title":           ev.title,
            "organizer_email": ev.organizer_email,
            "is_all_day":      ev.is_all_day,
            "start":           ev.start,
            "end":             ev.end,
        }
        for ev in raw_events
    ]

    def _classify(ev: dict):
        title = ev["title"]
        org   = ev["organizer_email"]
        is_ben = ("- בן" in title) or ("+ בן" in title) or (org in _BEN_EMAILS)
        is_tal = ("- טל" in title) or ("+ טל" in title) or (org in _TAL_EMAILS)
        covered = {n for n in _CHILDREN if n in title}
        return is_ben, is_tal, covered

    def _aw(dt):
        return _to_aware(dt) if dt else None

    ben_slots, tal_slots, neutral_child = [], [], []
    for ev in events:
        if ev["is_all_day"] or not ev["start"]:
            continue
        s = _aw(ev["start"])
        e = _aw(ev["end"]) if ev["end"] else s + timedelta(hours=1)
        is_ben, is_tal, covered = _classify(ev)
        if is_ben:
            ben_slots.append((s, e, covered, ev["title"]))
        if is_tal:
            tal_slots.append((s, e, covered, ev["title"]))
        if any(n in ev["title"] for n in _CHILDREN) and not is_ben and not is_tal:
            neutral_child.append((s, e, ev["title"]))

    alerts, seen = [], set()
    for bs, be, b_cov, b_title in ben_slots:
        for ts, te, t_cov, t_title in tal_slots:
            ov_s = max(bs, ts)
            ov_e = min(be, te)
            if ov_s >= ov_e:
                continue
            uncovered = _CHILDREN - (b_cov | t_cov)
            key = (ov_s, frozenset(uncovered))
            if not uncovered or key in seen:
                continue
            seen.add(key)
            child_str = " ו".join(sorted(uncovered))
            time_str  = f"{ov_s.strftime('%H:%M')}–{ov_e.strftime('%H:%M')}"
            if not b_cov and not t_cov:
                alerts.append(f"🧒 {time_str}: שני ההורים עסוקים — מי שומר על {child_str}?")
            else:
                alerts.append(
                    f"🧒 {time_str}: {child_str} ללא השגחה\n"
                    f"   בן: {b_title} | טל: {t_title}"
                )
    for cs, ce, ctitle in neutral_child:
        ben_busy = [t for bs, be, _, t in ben_slots if bs < ce and be > cs]
        tal_busy = [t for ts, te, _, t in tal_slots if ts < ce and te > cs]
        if ben_busy and tal_busy:
            alerts.append(f"⚠️ {ctitle}: שני ההורים עסוקים — בן: {', '.join(ben_busy)} | טל: {', '.join(tal_busy)}")
        elif ben_busy:
            alerts.append(f"⚠️ {ctitle}: בן עסוק ({', '.join(ben_busy)}) — טל מטפל/ת")
        elif tal_busy:
            alerts.append(f"⚠️ {ctitle}: טל עסוק/ה ({', '.join(tal_busy)}) — בן מטפל")

    if not alerts:
        return

    # Avoid duplicate childcare questions on the same day
    existing = db.get_pending_for_date(conn, today.isoformat(), day_before=False)
    already_asked = any("שמירה:" in r["title"] for r in existing)

    msg = "\n\n".join(alerts)
    if not already_asked:
        # Add a pending row so the answer is tracked
        row_id = db.add_pending_assignment(
            conn,
            f"childcare-{today.isoformat()}",
            "שמירה: ילדים ללא השגחה",
            today.isoformat(),
            chat_id,
        )
        await application.bot.send_message(chat_id, msg, reply_markup=kb_childcare(row_id))
    else:
        await application.bot.send_message(chat_id, msg)


async def morning_routine(application: Application) -> None:
    chat_id = config.TELEGRAM_GROUP_CHAT_ID
    if not chat_id:
        logger.warning("TELEGRAM_GROUP_CHAT_ID not set — skipping morning routine")
        return

    # Pending-assignment prompts come first so they don't get drowned out by the summary.
    today = datetime.now(config.TIMEZONE).date()
    tomorrow = today + timedelta(days=1)

    conn = application.bot_data["db"]
    caldav = application.bot_data["caldav"]

    # Lightweight wrapper to reuse _ask_pending_for_date helper signature.
    class _Ctx:
        bot = application.bot
        bot_data = application.bot_data
        application = application

    await _ask_pending_for_date(_Ctx(), chat_id, today.isoformat(), day_before=False)
    await _ask_pending_for_date(_Ctx(), chat_id, tomorrow.isoformat(), day_before=True)

    # Unified child-coverage check (replaces simple overlap check)
    await _check_child_coverage_and_notify(application, chat_id, today)

    # Logistics question — skip on Saturday (no school)
    if today.weekday() != 5:  # 5 = Saturday
        await application.bot.send_message(
            chat_id,
            "מי על הפיזורים והאיסופים של נועם ועמית היום?",
            reply_markup=kb_morning(),
        )

    # Send today's calendar summary
    class _Ctx2:
        bot = application.bot
        bot_data = application.bot_data
        application = application
    await _send_daily_summary(_Ctx2(), chat_id)


# ===================================================================
# Bootstrap
# ===================================================================
async def post_init(application: Application) -> None:
    conn = db.get_connection()
    db.init_db(conn)
    application.bot_data["db"] = conn

    caldav = CalDAVClient()
    try:
        await asyncio.wait_for(caldav.connect(), timeout=30.0)
        logger.info("CalDAV connected at startup.")
    except asyncio.TimeoutError:
        logger.warning("CalDAV connect timed out at startup — will reconnect on first use.")
    except Exception:
        logger.exception("CalDAV connect failed at startup — will reconnect on first use.")
    application.bot_data["caldav"] = caldav

    # Morning routine is triggered by GitHub Actions cron (morning_job.py).
    # APScheduler is kept running only to support future in-process jobs if needed,
    # but the morning_routine job is NOT scheduled here to avoid double-firing.
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    logger.info("Bot initialized. Morning routine handled by GitHub Actions cron.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled errors and continue (don't crash on network errors)."""
    logger.error("Update %s caused error %s", update, context.error, exc_info=context.error)


def build_application() -> Application:
    app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("cron_morning", cmd_cron_morning))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("testmorning", cmd_testmorning))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


def main() -> None:
    """Main entry point with automatic restart on crash."""
    import os
    import time
    retry_count = 0
    max_retries = 10
    retry_delay = 5  # seconds

    # On Cloud Run, webhook mode is required (run_webhook starts HTTP server on PORT).
    # Polling mode does not open a port, so Cloud Run health checks will fail.
    if os.environ.get("K_SERVICE") and not config.TELEGRAM_WEBHOOK_URL:
        logger.warning(
            "Running on Cloud Run without TELEGRAM_WEBHOOK_URL — "
            "the container will fail Cloud Run health checks. "
            "Set TELEGRAM_WEBHOOK_URL=https://<service-url>/webhook"
        )

    while True:
        try:
            app = build_application()
            if config.TELEGRAM_WEBHOOK_URL:
                logger.info("Starting in webhook mode on port %d", config.PORT)
                app.run_webhook(
                    listen="0.0.0.0",
                    port=config.PORT,
                    url_path="webhook",
                    webhook_url=config.TELEGRAM_WEBHOOK_URL,
                    secret_token=config.TELEGRAM_WEBHOOK_SECRET or None,
                )
            else:
                logger.info("Starting in polling mode (no TELEGRAM_WEBHOOK_URL set)")
                app.run_polling(allowed_updates=Update.ALL_TYPES)
            # If we reach here, polling/webhook stopped normally
            break
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            break
        except Exception as e:
            retry_count += 1
            logger.error("Bot crashed: %s (retry %d/%d in %ds)", e, retry_count, max_retries, retry_delay)
            if retry_count > max_retries:
                logger.critical("Max retries exceeded, giving up")
                raise
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
