"""MUSS Student Union — anonymous feedback bot for the presidential team.

Students send ideas, event suggestions, problems and messages anonymously.
The team receives them with a ticket number only, and can reply by simply
replying to the ticket message; the bot relays the answer without either side
seeing who the other is.
"""

import asyncio
import csv
import html
import io
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from db import DB

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
TEAM_NAME = os.environ.get("TEAM_NAME", "Presidential Team")
SCHOOL_NAME = os.environ.get("SCHOOL_NAME", "MUSS")
DB_PATH = os.environ.get("DB_PATH", "bot.db")
ROUTE_RETENTION_DAYS = int(os.environ.get("ROUTE_RETENTION_DAYS", "60"))

MAX_TEXT = 3000
MAX_CAPTION = 900
RATE_LIMIT = 5            # submissions ...
RATE_WINDOW = 10 * 60     # ... per 10 minutes

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# httpx logs full request URLs, which contain the bot token — keep it quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

db = DB(DB_PATH)

# key -> (menu button, header shown to the team, prompt shown to the student, noun)
CATEGORIES = {
    "idea": (
        "💡 Suggest an idea",
        "💡 New idea",
        "💡 <b>What would you like us to add or change at school?</b>\n\n"
        "Describe your idea — a new club, facility, rule, activity, anything. "
        "You can also send a photo with a caption.",
        "idea",
    ),
    "event": (
        "🎉 Event idea",
        "🎉 Event idea",
        "🎉 <b>What event should we organise?</b>\n\n"
        "Tell us what it is, who it's for and when it would be good to hold it.",
        "event idea",
    ),
    "problem": (
        "⚠️ Report a problem",
        "⚠️ Problem report",
        "⚠️ <b>What's the problem?</b>\n\n"
        "Describe what is happening and where. Please don't write the names of other "
        "students — describe the situation instead.",
        "report",
    ),
    "message": (
        "💬 Message the team",
        "💬 Message",
        "💬 <b>Write your message to the {team}.</b>\n\n"
        "Questions, feedback, thanks, complaints — anything goes.",
        "message",
    ),
}
BTN_POLLS = "🗳 Polls"
BTN_PRIVACY = "🔒 Is it anonymous?"
BTN_HELP = "❓ Help"
BTN_CANCEL = "❌ Cancel"

STATUS_LABELS = {"new": "🆕 New", "reviewing": "👀 Reviewing", "done": "✅ Done", "declined": "🙅 Declined"}
STATUS_NOTICE = {
    "reviewing": "👀 Good news — the {team} is now looking into your {noun} #{id}.",
    "done": "✅ Your {noun} #{id} has been marked as done. Thank you for helping improve our school!",
    "declined": "🙏 The {team} reviewed your {noun} #{id} but can't take it forward right now. "
                "Thank you for sharing it — keep the ideas coming!",
}

_rate: dict[int, deque] = defaultdict(deque)


# ---------------------------------------------------------------- helpers
def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id in ADMIN_IDS


def main_menu() -> ReplyKeyboardMarkup:
    c = CATEGORIES
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(c["idea"][0]), KeyboardButton(c["event"][0])],
            [KeyboardButton(c["problem"][0]), KeyboardButton(c["message"][0])],
            [KeyboardButton(BTN_POLLS), KeyboardButton(BTN_PRIVACY)],
            [KeyboardButton(BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Choose an option or just type your message…",
    )


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(BTN_CANCEL)]], resize_keyboard=True)


def category_for_button(text: str) -> str | None:
    for key, (button, *_rest) in CATEGORIES.items():
        if text == button:
            return key
    return None


def rate_limited(user_id: int) -> bool:
    now = time.time()
    q = _rate[user_id]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def fmt_date(ts: int) -> str:
    # Date only: exact times make it easier to guess who sent something.
    return datetime.fromtimestamp(ts).strftime("%d %b %Y")


def admin_keyboard(ticket_id: int, status: str) -> InlineKeyboardMarkup:
    def btn(key: str) -> InlineKeyboardButton:
        label = STATUS_LABELS[key]
        if key == status:
            label = "• " + label + " •"
        return InlineKeyboardButton(label, callback_data=f"st:{ticket_id}:{key}")

    return InlineKeyboardMarkup(
        [
            [btn("reviewing"), btn("done"), btn("declined")],
            [InlineKeyboardButton("🚫 Block sender (spam/abuse)", callback_data=f"blk:{ticket_id}")],
        ]
    )


def extract_content(message) -> tuple[str | None, str | None]:
    """Returns (text, photo_file_id) for a text or photo message."""
    if message.photo:
        return message.caption, message.photo[-1].file_id
    return message.text, None


async def send_content(bot, chat_id: int, header: str, text: str | None, photo_id: str | None,
                       footer: str = "", reply_markup=None):
    """Send user content WITHOUT parse mode, so nothing a student types can be interpreted as markup."""
    body = header + ("\n\n" + text if text else "") + ("\n\n" + footer if footer else "")
    if photo_id:
        return await bot.send_photo(chat_id, photo_id, caption=body[:1024], reply_markup=reply_markup)
    return await bot.send_message(chat_id, body, reply_markup=reply_markup)


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, ticket_id: int, header: str,
                        text: str | None, photo_id: str | None, with_buttons: bool,
                        exclude: int | None = None) -> None:
    ticket = db.get_ticket(ticket_id)
    footer = "↩️ Reply to this message to answer the student anonymously."
    markup = admin_keyboard(ticket_id, ticket["status"]) if with_buttons else None
    for admin_id in ADMIN_IDS:
        if admin_id == exclude:
            continue
        try:
            msg = await send_content(context.bot, admin_id, header, text, photo_id, footer, markup)
            db.add_link(admin_id, msg.message_id, ticket_id, "admin")
        except TelegramError as e:
            log.warning("Could not notify admin %s: %s", admin_id, e)


# ---------------------------------------------------------------- student side
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.add_user(update.effective_chat.id)
    context.user_data.clear()
    text = (
        f"👋 <b>Welcome to the {html.escape(SCHOOL_NAME)} Student Union bot!</b>\n\n"
        f"This is a direct, <b>100% anonymous</b> line to your {html.escape(TEAM_NAME)}. "
        "We want to hear what <i>you</i> want for our school.\n\n"
        "<b>What would you like to do?</b>\n"
        "💡 <b>Suggest an idea</b> — something new to add or change at school\n"
        "🎉 <b>Event idea</b> — an event, trip or activity you'd love to see\n"
        "⚠️ <b>Report a problem</b> — something that isn't working or isn't fair\n"
        "💬 <b>Message the team</b> — questions, feedback or thanks\n"
        "🗳 <b>Polls</b> — vote on what we should do next\n\n"
        "👇 Tap a button below, or simply type your message and I'll ask what it's about.\n\n"
        "🔒 The team <b>never</b> sees your name, username or profile. "
        "Tap “Is it anonymous?” to learn how."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu())
    if is_admin(update):
        await update.message.reply_text("🛠 You're a team admin. Send /admin to see team commands.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "❓ <b>How to use this bot</b>\n\n"
        "1. Choose what you want to send from the menu (or just type).\n"
        "2. Write your message — you can attach one photo.\n"
        "3. Check the preview and tap <b>Send anonymously</b>.\n\n"
        "You'll get a ticket number. If the team answers, the reply appears right here — "
        "you can <b>reply to their message</b> to continue the conversation, still anonymously.\n\n"
        "<b>Commands</b>\n"
        "/start — main menu\n"
        "/polls — open polls\n"
        "/privacy — how anonymity works\n"
        "/forgetme — delete the link between you and your past messages\n"
        "/cancel — cancel what you're writing"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu())


async def privacy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🔒 <b>How anonymity works</b>\n\n"
        "• The team <b>never</b> sees your name, username, phone number or profile picture — "
        "only your message and a ticket number like #12.\n"
        "• Your messages are <b>copied</b> by the bot, not forwarded, so your account is never attached.\n"
        "• Only the date is shown, not the exact time.\n"
        "• Poll votes are saved as a scrambled code, not as your account — nobody can see what you voted.\n"
        "• To let the team reply to you, the bot privately remembers which chat a ticket came from. "
        f"This link is deleted automatically after {ROUTE_RETENTION_DAYS} days, "
        "or right now if you send /forgetme.\n\n"
        "⚠️ <b>Stay anonymous:</b> don't write your name, class or details only you would know. "
        "Voice messages, videos and files aren't accepted because they could reveal who you are."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu())


async def forgetme_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.forget(update.effective_chat.id)
    context.user_data.clear()
    await update.message.reply_text(
        "🧹 Done. Your chat is no longer linked to any message you sent, and you've been removed "
        "from announcements. The team can't reply to your old tickets anymore.\n\n"
        "Send /start any time to come back.",
        reply_markup=main_menu(),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text("Cancelled. What would you like to do next? 👇", reply_markup=main_menu())


async def ask_for_content(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str) -> None:
    context.user_data.clear()
    context.user_data["mode"] = category
    prompt = CATEGORIES[category][2].format(team=html.escape(TEAM_NAME))
    await update.message.reply_text(prompt, parse_mode=ParseMode.HTML, reply_markup=cancel_menu())


async def show_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ud = context.user_data
    header = f"👀 Preview — this is exactly what the team will see ({CATEGORIES[ud['category']][1]}):"
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Send anonymously", callback_data="send")],
            [InlineKeyboardButton("✏️ Rewrite", callback_data=f"cat:{ud['category']}"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]
    )
    await update.message.reply_text("Almost done…", reply_markup=main_menu())
    await send_content(context.bot, update.effective_chat.id, header, ud.get("text"), ud.get("photo_id"),
                       reply_markup=markup)


async def ask_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = [[InlineKeyboardButton(CATEGORIES[k][0], callback_data=f"pick:{k}")] for k in CATEGORIES]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await update.message.reply_text("What is this message about?", reply_markup=InlineKeyboardMarkup(rows))


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    chat_id = update.effective_chat.id
    db.add_user(chat_id)
    text = msg.text or ""

    # 1) Team member replying to a ticket -> relay to the student.
    if is_admin(update) and msg.reply_to_message:
        ticket_id = db.find_link(chat_id, msg.reply_to_message.message_id, "admin")
        if ticket_id:
            await relay_team_reply(update, context, ticket_id)
            return

    # 2) Menu buttons.
    if text == BTN_CANCEL:
        await cancel_cmd(update, context)
        return
    if text == BTN_POLLS:
        await polls_cmd(update, context)
        return
    if text == BTN_PRIVACY:
        await privacy_cmd(update, context)
        return
    if text == BTN_HELP:
        await help_cmd(update, context)
        return
    category = category_for_button(text)
    if category:
        await ask_for_content(update, context, category)
        return

    # 3) Student replying to a team answer -> follow-up on the same ticket.
    if msg.reply_to_message:
        ticket_id = db.find_link(chat_id, msg.reply_to_message.message_id, "student")
        if ticket_id:
            await relay_student_followup(update, context, ticket_id)
            return

    # 4) New content.
    content, photo_id = extract_content(msg)
    if not content and not photo_id:
        return
    if content and len(content) > (MAX_CAPTION if photo_id else MAX_TEXT):
        limit = MAX_CAPTION if photo_id else MAX_TEXT
        await msg.reply_text(f"That's a bit long — please keep it under {limit} characters.")
        return
    if not photo_id and len(content.strip()) < 5:
        await msg.reply_text("Could you write a little more? 🙂", reply_markup=main_menu())
        return

    ud = context.user_data
    ud["text"], ud["photo_id"] = content, photo_id
    if ud.get("mode") in CATEGORIES:
        ud["category"] = ud.pop("mode")
        await show_preview(update, context)
    else:
        await ask_category(update, context)


async def unsupported_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔒 To protect your anonymity I only accept text and photos "
        "(voices, videos and files can reveal who you are). Please type your message instead.",
        reply_markup=main_menu(),
    )


async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    ud = context.user_data
    chat_id = update.effective_chat.id
    if "category" not in ud or (not ud.get("text") and not ud.get("photo_id")):
        await query.answer("This draft expired — please write it again.", show_alert=True)
        return
    if db.is_blocked(chat_id):
        await query.answer("You can't send messages right now.", show_alert=True)
        return
    if rate_limited(update.effective_user.id):
        await query.answer("You're sending a lot of messages — please wait a few minutes. 🙏", show_alert=True)
        return

    db.purge_old_routes(ROUTE_RETENTION_DAYS)
    category = ud["category"]
    text, photo_id = ud.get("text"), ud.get("photo_id")
    ticket_id = db.create_ticket(chat_id, category, text, photo_id)
    ud.clear()
    await query.answer("Sent! ✅")
    await query.edit_message_reply_markup(None)

    header = f"{CATEGORIES[category][1]} · #{ticket_id}\n📅 {fmt_date(int(time.time()))}"
    await notify_admins(context, ticket_id, header, text, photo_id, True)

    noun = CATEGORIES[category][3]
    await context.bot.send_message(
        chat_id,
        f"✅ <b>Your {noun} was sent anonymously!</b>\n\n"
        f"Ticket: <b>#{ticket_id}</b>\n"
        f"The {html.escape(TEAM_NAME)} will read it soon. If they reply, you'll see it here — "
        "and you can reply back to continue the conversation.\n\n"
        "Anything else? 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def relay_student_followup(update: Update, context: ContextTypes.DEFAULT_TYPE, ticket_id: int) -> None:
    msg = update.message
    if db.is_blocked(update.effective_chat.id):
        await msg.reply_text("You can't send messages right now.")
        return
    if rate_limited(update.effective_user.id):
        await msg.reply_text("You're sending a lot of messages — please wait a few minutes. 🙏")
        return
    text, photo_id = extract_content(msg)
    if not text and not photo_id:
        return
    db.add_message(ticket_id, "student", text, photo_id)
    await notify_admins(context, ticket_id, f"💬 Follow-up from student · #{ticket_id}", text, photo_id, False)
    await msg.reply_text("✅ Sent to the team anonymously.", reply_markup=main_menu())


async def relay_team_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, ticket_id: int) -> None:
    msg = update.message
    ticket = db.get_ticket(ticket_id)
    if not ticket or ticket["chat_id"] is None:
        await msg.reply_text(
            f"⚠️ Can't deliver: the student's reply link for #{ticket_id} has expired or they used /forgetme."
        )
        return
    text, photo_id = extract_content(msg)
    if not text and not photo_id:
        await msg.reply_text("Only text and photo replies can be relayed.")
        return
    try:
        sent = await send_content(
            context.bot, ticket["chat_id"],
            f"📩 Reply from the {TEAM_NAME} (about your ticket #{ticket_id}):",
            text, photo_id,
            footer="↩️ Reply to this message to answer back — you're still anonymous.",
        )
    except Forbidden:
        await msg.reply_text(f"⚠️ Can't deliver to #{ticket_id}: the student has blocked the bot.")
        return
    db.add_link(ticket["chat_id"], sent.message_id, ticket_id, "student")
    db.add_message(ticket_id, "team", text, photo_id)
    if ticket["status"] == "new":
        db.set_status(ticket_id, "reviewing")
    await msg.reply_text(f"✅ Delivered anonymously to ticket #{ticket_id}.")
    # Keep other team members in the loop.
    await notify_admins(context, ticket_id, f"↩️ Team reply sent on #{ticket_id}:", text, photo_id, False,
                        exclude=update.effective_user.id)


# ---------------------------------------------------------------- polls
def poll_text(poll, show_results: bool) -> str:
    options = poll["options"].split("\n")
    lines = [f"🗳 Poll #{poll['id']}" + ("" if poll["open"] else " (closed)"), "", poll["question"]]
    if show_results:
        results = db.poll_results(poll["id"])
        total = sum(results.values())
        lines.append("")
        for i, opt in enumerate(options):
            n = results.get(i, 0)
            pct = round(100 * n / total) if total else 0
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            lines.append(f"{opt}\n{bar} {pct}% ({n})")
        lines.append(f"\nTotal votes: {total}")
    return "\n".join(lines)


def poll_keyboard(poll) -> InlineKeyboardMarkup:
    options = poll["options"].split("\n")
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(opt, callback_data=f"vote:{poll['id']}:{i}")] for i, opt in enumerate(options)]
    )


async def polls_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    polls = db.list_polls(only_open=True)
    if not polls:
        await update.message.reply_text(
            "There are no open polls right now. We'll let you know when there's something to vote on! 🗳",
            reply_markup=main_menu(),
        )
        return
    for poll in polls:
        voted = db.has_voted(poll["id"], update.effective_user.id)
        await update.message.reply_text(
            poll_text(poll, show_results=voted) + ("\n\n✅ You already voted." if voted else ""),
            reply_markup=None if voted else poll_keyboard(poll),
        )


async def on_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, poll_id, option = query.data.split(":")
    poll = db.get_poll(int(poll_id))
    if not poll or not poll["open"]:
        await query.answer("This poll is closed.", show_alert=True)
        await query.edit_message_reply_markup(None)
        return
    if not db.vote(poll["id"], update.effective_user.id, int(option)):
        await query.answer("You already voted in this poll.", show_alert=True)
    else:
        await query.answer("Vote counted anonymously ✅")
    await query.edit_message_text(poll_text(poll, show_results=True) + "\n\n✅ You voted.")


# ---------------------------------------------------------------- callbacks
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    ud = context.user_data

    if data == "send":
        await submit(update, context)
    elif data == "cancel":
        ud.clear()
        await query.answer("Cancelled")
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(update.effective_chat.id, "Cancelled. What next? 👇",
                                       reply_markup=main_menu())
    elif data.startswith("pick:"):
        category = data.split(":")[1]
        if not ud.get("text") and not ud.get("photo_id"):
            await query.answer("This draft expired — please write it again.", show_alert=True)
            return
        ud["category"] = category
        await query.answer()
        await query.delete_message()
        header = f"👀 Preview — this is exactly what the team will see ({CATEGORIES[category][1]}):"
        markup = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Send anonymously", callback_data="send")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]
        )
        await send_content(context.bot, update.effective_chat.id, header, ud.get("text"), ud.get("photo_id"),
                           reply_markup=markup)
    elif data.startswith("cat:"):
        category = data.split(":")[1]
        await query.answer()
        await query.edit_message_reply_markup(None)
        ud.clear()
        ud["mode"] = category
        prompt = CATEGORIES[category][2].format(team=html.escape(TEAM_NAME))
        await context.bot.send_message(update.effective_chat.id, prompt, parse_mode=ParseMode.HTML,
                                       reply_markup=cancel_menu())
    elif data.startswith("vote:"):
        await on_vote(update, context)
    elif data.startswith("st:") and is_admin(update):
        await on_status(update, context)
    elif data.startswith("blk:") and is_admin(update):
        await on_block(update, context)
    else:
        await query.answer()


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, ticket_id, status = query.data.split(":")
    ticket_id = int(ticket_id)
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await query.answer("Ticket not found.")
        return
    if ticket["status"] == status:
        await query.answer(f"Already {STATUS_LABELS[status]}")
        return
    db.set_status(ticket_id, status)
    await query.answer(f"#{ticket_id} → {STATUS_LABELS[status]}")
    await query.edit_message_reply_markup(admin_keyboard(ticket_id, status))
    if ticket["chat_id"] is not None and status in STATUS_NOTICE:
        notice = STATUS_NOTICE[status].format(team=TEAM_NAME, noun=CATEGORIES[ticket["category"]][3], id=ticket_id)
        try:
            await context.bot.send_message(ticket["chat_id"], notice)
        except TelegramError:
            pass


async def on_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    ticket_id = int(query.data.split(":")[1])
    ticket = db.get_ticket(ticket_id)
    if not ticket or ticket["chat_id"] is None:
        await query.answer("Can't block: the link for this ticket has expired.", show_alert=True)
        return
    db.set_blocked(ticket["chat_id"], True)
    db.set_status(ticket_id, "declined")
    await query.answer(f"Sender of #{ticket_id} blocked. Use /unblock {ticket_id} to undo.", show_alert=True)
    await query.edit_message_reply_markup(admin_keyboard(ticket_id, "declined"))


# ---------------------------------------------------------------- admin commands
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update):
            await update.message.reply_text("This command is for the team only.")
            return
        return await func(update, context)
    return wrapper


@admin_only
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🛠 <b>Team commands</b>\n\n"
        "<b>Replying:</b> just <i>reply</i> to any ticket message — the bot relays it anonymously.\n"
        "Use the buttons under a ticket to set 👀 Reviewing / ✅ Done / 🙅 Declined "
        "(the student is notified) or 🚫 block a spammer.\n\n"
        "/stats — numbers at a glance\n"
        "/ticket 12 — full conversation of ticket #12\n"
        "/export — download all tickets as a spreadsheet (CSV)\n"
        "/newpoll Question | Option 1 | Option 2 | … — send a poll to everyone\n"
        "/results — results of all polls\n"
        "/closepoll 3 — close poll #3\n"
        "/broadcast Your text — announcement to all students\n"
        "/unblock 12 — unblock the sender of ticket #12",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = db.stats()
    cats = "\n".join(f"  {CATEGORIES[k][1]}: {v}" for k, v in s["by_category"].items() if k in CATEGORIES) or "  —"
    statuses = "\n".join(f"  {STATUS_LABELS.get(k, k)}: {v}" for k, v in s["by_status"].items()) or "  —"
    await update.message.reply_text(
        f"📊 Stats\n\n👥 Students using the bot: {s['users']}\n📨 Tickets: {s['tickets']}\n\n"
        f"By type:\n{cats}\n\nBy status:\n{statuses}\n\n🗳 Open polls: {s['open_polls']}"
    )


@admin_only
async def ticket_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Usage: /ticket 12")
        return
    ticket_id = int(context.args[0].lstrip("#"))
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await update.message.reply_text("Ticket not found.")
        return
    lines = [f"{CATEGORIES[ticket['category']][1]} · #{ticket_id} · {STATUS_LABELS.get(ticket['status'])}",
             f"📅 {fmt_date(ticket['created_at'])}", ""]
    for m in db.ticket_messages(ticket_id):
        who = "🧑‍🎓 Student" if m["sender"] == "student" else "🏛 Team"
        lines.append(f"{who} ({fmt_date(m['created_at'])}): {m['text'] or ''}{' [photo]' if m['photo_id'] else ''}")
    msg = await update.message.reply_text("\n".join(lines)[:4000], reply_markup=admin_keyboard(ticket_id, ticket["status"]))
    db.add_link(update.effective_chat.id, msg.message_id, ticket_id, "admin")


@admin_only
async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ticket", "date", "type", "status", "message", "team_replies"])
    for t in db.all_tickets():
        writer.writerow([t["id"], fmt_date(t["created_at"]), t["category"], t["status"], t["text"] or "", t["replies"]])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # BOM so Excel opens it correctly
    await update.message.reply_document(data, filename=f"tickets_{datetime.now():%Y-%m-%d}.csv",
                                        caption="📎 All tickets (no identities included).")


async def _broadcast(context: ContextTypes.DEFAULT_TYPE, send) -> tuple[int, int]:
    ok = failed = 0
    for chat_id in db.active_users():
        try:
            await send(chat_id)
            ok += 1
        except Forbidden:
            db.remove_user(chat_id)
            failed += 1
        except TelegramError as e:
            log.warning("Broadcast failed: %s", e)
            failed += 1
        await asyncio.sleep(0.05)  # stay well under Telegram's rate limits
    return ok, failed


@admin_only
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /broadcast Your announcement text")
        return
    await update.message.reply_text("📣 Sending…")
    ok, failed = await _broadcast(
        context, lambda cid: context.bot.send_message(cid, f"📣 Announcement from the {TEAM_NAME}\n\n{text}")
    )
    await update.message.reply_text(f"✅ Announcement delivered to {ok} students ({failed} unreachable).")


@admin_only
async def newpoll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = update.message.text.partition(" ")[2]
    parts = [p.strip() for p in raw.split("|") if p.strip()]
    if len(parts) < 3 or len(parts) > 11:
        await update.message.reply_text(
            "Usage: /newpoll Question | Option 1 | Option 2 | …\n"
            "Example: /newpoll Which club should we start? | Robotics | Chess | Debate\n"
            "(2–10 options)"
        )
        return
    question, options = parts[0], [o[:60] for o in parts[1:]]
    poll_id = db.create_poll(question, options)
    poll = db.get_poll(poll_id)
    await update.message.reply_text(f"🗳 Poll #{poll_id} created. Sending to all students…")
    ok, failed = await _broadcast(
        context, lambda cid: context.bot.send_message(cid, poll_text(poll, False), reply_markup=poll_keyboard(poll))
    )
    await update.message.reply_text(
        f"✅ Poll #{poll_id} sent to {ok} students ({failed} unreachable).\n"
        f"See results with /results, close it with /closepoll {poll_id}."
    )


@admin_only
async def results_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    polls = db.list_polls()
    if not polls:
        await update.message.reply_text("No polls yet. Create one with /newpoll.")
        return
    for poll in polls[:10]:
        await update.message.reply_text(poll_text(poll, True))


@admin_only
async def closepoll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit() or not db.get_poll(int(context.args[0])):
        await update.message.reply_text("Usage: /closepoll 3")
        return
    poll_id = int(context.args[0])
    db.close_poll(poll_id)
    await update.message.reply_text("🔒 Closed.\n\n" + poll_text(db.get_poll(poll_id), True))


@admin_only
async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Usage: /unblock 12  (ticket number)")
        return
    ticket = db.get_ticket(int(context.args[0].lstrip("#")))
    if not ticket or ticket["chat_id"] is None:
        await update.message.reply_text("Ticket not found or its link has expired.")
        return
    db.set_blocked(ticket["chat_id"], False)
    await update.message.reply_text(f"✅ Sender of #{ticket['id']} unblocked.")


# ---------------------------------------------------------------- main
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error: %s", context.error, exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN or not ADMIN_IDS:
        raise SystemExit("Set BOT_TOKEN and ADMIN_IDS in your .env file (see .env.example).")

    db.purge_old_routes(ROUTE_RETENTION_DAYS)
    app = Application.builder().token(BOT_TOKEN).build()
    private = filters.ChatType.PRIVATE

    for name, handler in [
        ("start", start), ("help", help_cmd), ("privacy", privacy_cmd), ("forgetme", forgetme_cmd),
        ("cancel", cancel_cmd), ("polls", polls_cmd),
        ("admin", admin_cmd), ("stats", stats_cmd), ("ticket", ticket_cmd), ("export", export_cmd),
        ("broadcast", broadcast_cmd), ("newpoll", newpoll_cmd), ("results", results_cmd),
        ("closepoll", closepoll_cmd), ("unblock", unblock_cmd),
    ]:
        app.add_handler(CommandHandler(name, handler, filters=private))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(private & (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, on_private_message))
    app.add_handler(MessageHandler(private & ~filters.COMMAND & ~filters.TEXT & ~filters.PHOTO, unsupported_media))
    app.add_error_handler(on_error)

    log.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
