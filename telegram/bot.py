"""Hush Enquiry Catcher: Telegram demo, version 1.

What it does:
  1. A "customer" messages the bot, e.g. "I've got wasps".
  2. The AI (Claude Haiku, via the Claude API) asks what the pest is, where, the postcode,
     a name and a phone number.
  3. It quotes ONLY from the price list in business.json.
  4. It offers free time slots and books one (saved in bookings.json).
  5. The owner gets a Telegram message with the booking.
  6. Anything unusual (bed bugs, commercial jobs, complaints...) goes to the owner instead.

Run it on your laptop:  python bot.py
Commands in the chat:   /start  /reset  /bookings (owner only)
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
import anthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(Path(__file__).parent / "bot.log")],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("hush")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")  # fast and cheap
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "").strip()  # your own Telegram chat id

HERE = Path(__file__).parent
BUSINESS = json.loads((HERE / "business.json").read_text())
BOOKINGS_FILE = HERE / "bookings.json"


# ---------- bookings (a simple JSON file) ----------

def load_bookings() -> list[dict]:
    if BOOKINGS_FILE.exists():
        return json.loads(BOOKINGS_FILE.read_text())
    return []


def save_bookings(bookings: list[dict]) -> None:
    BOOKINGS_FILE.write_text(json.dumps(bookings, indent=2))


def free_slots(today: date | None = None) -> list[str]:
    """Every free slot over the next few working days, e.g. 'Wed 26 Nov, 10:00-12:00'."""
    today = today or date.today()
    taken = {b["slot"] for b in load_bookings()}
    slots, day, found_days = [], today, 0
    while found_days < BUSINESS["days_ahead"]:
        day += timedelta(days=1)  # start from tomorrow
        if day.strftime("%a") not in BUSINESS["working_days"]:
            continue
        found_days += 1
        for t in BUSINESS["time_slots"]:
            label = f"{day.strftime('%a %d %b')}, {t}"
            if label not in taken:
                slots.append(label)
    return slots


# ---------- the AI's tools (one set per chat, so we know who is talking) ----------

def make_tools(chat_id: int, outbox: list[str]):
    """outbox collects messages for the owner; they're sent after the AI finishes its reply."""

    def get_free_slots() -> dict:
        """Get the free appointment slots for the next few working days. Offer the customer 2 or 3 of them."""
        return {"free_slots": free_slots()[:8]}

    def book_job(name: str, phone: str, postcode: str, pest: str, where_in_property: str,
                 slot: str, price: str, urgent: bool = False, notes: str = "") -> dict:
        """Book the job once the customer has agreed a slot and the price.

        Args:
            name: customer's name
            phone: customer's phone number
            postcode: full postcode of the property
            pest: the pest, using the name from the price list
            where_in_property: where the problem is, e.g. 'roof edge at the back'
            slot: one slot exactly as returned by get_free_slots
            price: the price quoted, exactly as on the price list
            urgent: true if the job matches the urgent rules
            notes: anything else useful, e.g. children, pets, allergies, access
        """
        if slot not in free_slots():
            return {"booked": False, "reason": "That slot is no longer free. Call get_free_slots and offer others."}
        bookings = load_bookings()
        booking = {
            "ref": f"NV{len(bookings) + 1:04d}",
            "created": datetime.now().isoformat(timespec="minutes"),
            "chat_id": chat_id, "name": name, "phone": phone, "postcode": postcode.upper(),
            "pest": pest, "where": where_in_property, "slot": slot, "price": price,
            "urgent": urgent, "notes": notes,
        }
        bookings.append(booking)
        save_bookings(bookings)
        flag = "🔴 URGENT " if urgent else ""
        outbox.append(
            f"{flag}✅ New booking {booking['ref']}\n"
            f"Job: {pest}, {where_in_property}\n"
            f"Where: {booking['postcode']}\n"
            f"When: {slot}\n"
            f"Price: {price}\n"
            f"Customer: {name}, {phone}\n"
            + (f"Notes: {notes}" if notes else "")
        )
        log.info("Booked %s", booking["ref"])
        return {"booked": True, "ref": booking["ref"]}

    def hand_to_owner(reason: str, summary: str, name: str = "", phone: str = "", urgent: bool = False) -> dict:
        """Pass the enquiry to the owner instead of booking it. Use for anything in the
        'always hand to owner' list, anything you are unsure about, or if the customer asks for a person.

        Args:
            reason: short reason, e.g. 'bed bugs need a survey'
            summary: what the customer needs, in one or two sentences
            name: customer's name if known
            phone: customer's phone number if known
            urgent: true if it matches the urgent rules
        """
        flag = "🔴 URGENT " if urgent else ""
        outbox.append(
            f"{flag}📞 Needs you: {reason}\n{summary}\n"
            f"Customer: {name or 'unknown'}, {phone or 'no number yet'}"
        )
        log.info("Handed to owner: %s", reason)
        return {"passed_to_owner": True}

    return [get_free_slots, book_job, hand_to_owner]


def system_prompt() -> str:
    b = BUSINESS
    prices = "\n".join(f"- {p['pest']}: {p['price']} ({p['notes']})" for p in b["prices"])
    return f"""You are Hush, the messaging assistant for {b['company_name']}, a UK pest control company.
A customer has messaged because they couldn't get through on the phone. Today is {date.today().strftime('%A %d %B %Y')}.

Style: friendly, calm, British, short. Like a text message: one or two sentences, one question at a time. No long lists. If asked, say you're the company's automated assistant and the owner can step in.

About the business:
- Areas covered: {b['areas_covered']}
- Opening hours: {b['opening_hours']}
- Technician: {b['technician']}

Price list (the ONLY prices you may give, word for word):
{prices}

Always hand to the owner (use hand_to_owner, never book these): {b['always_hand_to_owner']}.
Urgent means: {b['urgent_means']}. For urgent jobs, still book the earliest slot if it's on the price list, and mark urgent=true.

How to handle a chat:
1. Find out the pest and where it is in the property.
2. If it's on the price list, give the price. Never invent, discount or guess a price.
3. Ask for the postcode. If it's outside the areas covered, say sorry, you don't cover that area, and stop.
4. Call get_free_slots and offer 2 or 3 slots.
5. Get their name and phone number, then call book_job with the exact slot text.
6. Confirm the booking with the reference and say {b['technician']} will text when he's on his way.
If anything is unclear or not covered here, use hand_to_owner and tell the customer the owner will be in touch shortly."""


# ---------- one Claude conversation per Telegram chat ----------

TOOLS = [
    {
        "name": "get_free_slots",
        "description": "Get the free appointment slots for the next few working days. Offer the customer 2 or 3 of them.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "book_job",
        "description": "Book the job once the customer has agreed a slot and the price.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "customer's name"},
                "phone": {"type": "string", "description": "customer's phone number"},
                "postcode": {"type": "string", "description": "full postcode of the property"},
                "pest": {"type": "string", "description": "the pest, using the name from the price list"},
                "where_in_property": {"type": "string", "description": "where the problem is, e.g. 'roof edge at the back'"},
                "slot": {"type": "string", "description": "one slot exactly as returned by get_free_slots"},
                "price": {"type": "string", "description": "the price quoted, exactly as on the price list"},
                "urgent": {"type": "boolean", "description": "true if the job matches the urgent rules"},
                "notes": {"type": "string", "description": "anything else useful: children, pets, allergies, access"},
            },
            "required": ["name", "phone", "postcode", "pest", "where_in_property", "slot", "price"],
        },
    },
    {
        "name": "hand_to_owner",
        "description": ("Pass the enquiry to the owner instead of booking it. Use for anything in the "
                        "'always hand to owner' list, anything you are unsure about, or if the customer asks for a person."),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "short reason, e.g. 'bed bugs need a survey'"},
                "summary": {"type": "string", "description": "what the customer needs, in one or two sentences"},
                "name": {"type": "string", "description": "customer's name if known"},
                "phone": {"type": "string", "description": "customer's phone number if known"},
                "urgent": {"type": "boolean", "description": "true if it matches the urgent rules"},
            },
            "required": ["reason", "summary"],
        },
    },
]

client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, max_retries=4, timeout=60) if ANTHROPIC_API_KEY else None
chats: dict[int, dict] = {}  # chat_id -> {"messages": [...], "outbox": [...], "tools": {...}}


def get_chat(chat_id: int) -> dict:
    if chat_id not in chats:
        outbox: list[str] = []
        funcs = {f.__name__: f for f in make_tools(chat_id, outbox)}
        chats[chat_id] = {"messages": [], "outbox": outbox, "tools": funcs, "system": system_prompt()}
    return chats[chat_id]


def run_tool(session: dict, name: str, args: dict) -> tuple[str, bool]:
    """Run one of our Python tools. Returns (result as JSON text, is_error)."""
    func = session["tools"].get(name)
    if func is None:
        return f"Unknown tool {name}", True
    try:
        return json.dumps(func(**args)), False
    except TypeError as e:  # missing or wrong arguments
        return f"Error: {e}. Check the arguments and try again.", True


async def ask_ai(chat_id: int, text: str) -> str:
    """Send the customer's message to Claude, run any tools it asks for, return its reply."""
    session = get_chat(chat_id)
    messages = session["messages"]
    start = len(messages)
    messages.append({"role": "user", "content": text})
    try:
        return await _ask_loop(session, messages)
    except BaseException:
        del messages[start:]  # forget the half-finished turn so the next message starts clean
        raise


async def _ask_loop(session: dict, messages: list) -> str:
    for _ in range(8):  # at most 8 tool rounds per message
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            system=session["system"],
            tools=TOOLS,
            messages=messages,
        )
        # keep Claude's whole reply (including any thinking) in the history, unchanged
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "refusal":
            return "Sorry, I can't help with that here. The owner will be in touch."
        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_calls:
            reply = "\n".join(b.text for b in response.content if b.type == "text").strip()
            return reply or "Sorry, could you say that again?"
        results = []
        for call in tool_calls:
            log.info("Tool %s %s", call.name, call.input)
            content, is_error = run_tool(session, call.name, dict(call.input))
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return "Sorry, I got a bit stuck there. The owner will get back to you shortly."


async def tell_owner(app: Application, text: str) -> None:
    if OWNER_CHAT_ID == TELEGRAM_BOT_TOKEN.split(":")[0]:
        log.warning("OWNER_CHAT_ID is the bot's own id. Use YOUR id (shown after /start). Owner message not sent:\n%s", text)
        return
    if not OWNER_CHAT_ID.lstrip("-").isdigit():
        log.warning("OWNER_CHAT_ID must be your chat id NUMBER (not the bot's name). "
                    "Send /start and copy the number shown here. Owner message not sent:\n%s", text)
        return
    await app.bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text)


# ---------- Telegram handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chats.pop(update.effective_chat.id, None)
    await update.message.reply_text(
        f"Hi, sorry we missed your call, it's {BUSINESS['company_name']}. "
        "Is it wasps, mice or something else?"
    )
    log.info("Chat started. Your chat id is %s (put it in OWNER_CHAT_ID to get booking alerts)",
             update.effective_chat.id)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chats.pop(update.effective_chat.id, None)
    await update.message.reply_text("Chat reset. Send /start to begin again.")


async def bookings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if str(update.effective_chat.id) != OWNER_CHAT_ID:
        return
    rows = load_bookings()
    if not rows:
        await update.message.reply_text("No bookings yet.")
        return
    lines = [f"{b['ref']} · {b['slot']} · {b['pest']} · {b['postcode']} · {b['price']}" for b in rows[-15:]]
    await update.message.reply_text("Latest bookings:\n" + "\n".join(lines))


async def keep_typing(bot, chat_id: int) -> None:
    """Show 'typing...' in Telegram until the reply is ready (it lasts 5s each time)."""
    while True:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        await asyncio.sleep(4)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = update.message.text
    log.info("Message from %s: %s", chat_id, text)
    typing = asyncio.create_task(keep_typing(context.bot, chat_id))
    outbox: list[str] = []
    try:
        session = get_chat(chat_id)
        outbox = session["outbox"]
        reply = await asyncio.wait_for(ask_ai(chat_id, text), timeout=90)
    except asyncio.TimeoutError:
        log.error("Claude took longer than 90 seconds")
        reply = "Sorry, that's taking a while. Could you send that again?"
    except anthropic.AuthenticationError:
        log.error("Your ANTHROPIC_API_KEY is wrong. Check .env")
        reply = "Sorry, I'm having trouble right now. The owner will get back to you shortly."
        outbox.append("⚠️ Hush error: the Claude API key in .env is wrong.")
    except Exception as e:  # e.g. no credit left, model name wrong, Claude overloaded
        log.exception("Claude error")
        reply = "Sorry, I'm having trouble right now. The owner will get back to you shortly."
        outbox.append(f"⚠️ Hush error: {e.__class__.__name__}: {str(e)[:300]}")
    finally:
        typing.cancel()
    log.info("Reply to %s: %s", chat_id, reply)
    await update.message.reply_text(reply)
    while outbox:
        try:
            await tell_owner(context.application, outbox.pop(0))
        except Exception:
            log.exception("Could not send the owner alert")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Something went wrong", exc_info=context.error)


def main() -> None:
    missing = [k for k, v in {"TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY}.items() if not v]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)} (see .env.example)")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("bookings", bookings_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    log.info("Using Claude model %s", CLAUDE_MODEL)
    log.info("Hush is running. Open your bot in Telegram and send /start. Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
