"""Hush: an AI phone receptionist for pest control businesses.

How a call flows:
  1. Someone rings the Twilio number. Twilio asks POST /voice what to do.
  2. /voice tells Twilio to open a ConversationRelay WebSocket to /ws.
     Twilio turns the caller's speech into text and reads our replies aloud.
  3. /ws sends each thing the caller says to Claude, and streams Claude's
     reply back to Twilio word by word so there are no long pauses.
  4. When Hush has the caller's details it calls the send_job_to_owner tool,
     which texts the job to the business owner (marked URGENT if needed).
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from xml.sax.saxutils import quoteattr

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hush")

MODEL = os.environ.get("HUSH_MODEL", "claude-haiku-5-5")  # fast model = short pauses on the phone
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # e.g. https://hush.onrender.com
OWNER_PHONE = os.environ.get("OWNER_PHONE", "")  # the owner's mobile, e.g. +447700900123
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER = os.environ.get("TWILIO_NUMBER", "")  # your Twilio number, used to send the texts

BUSINESS = json.loads((Path(__file__).parent / "business.json").read_text())

WELCOME = (
    f"Hello, you're through to {BUSINESS['company_name']}. "
    "I'm Hush, the assistant. How can I help?"
)

SYSTEM_PROMPT = f"""You are Hush, the phone receptionist for {BUSINESS['company_name']}, a UK pest control company. You are speaking on a live phone call. Everything you write is read aloud by a text-to-speech voice.

How you sound: warm, calm, British and to the point, like a friendly office manager. Keep each reply to one or two short sentences and ask one question at a time. Write plain spoken English only: no lists, no bullet points, no emoji, no symbols. Say prices as words a person would say, such as "seventy-five pounds". If someone asks whether you are a person, say you are the company's assistant.

About the business:
- Services: {BUSINESS['services']}
- Areas covered: {BUSINESS['areas_covered']}
- Opening hours: {BUSINESS['opening_hours']}
- Price guide: {BUSINESS['price_guide']}

You have already answered the call by saying: "{WELCOME}"

Your job is to collect these details, naturally, one at a time:
the caller's name, the best phone number to reach them (offer to use the number they're calling from), the address and postcode, what pest it is, where it is, how long it has been going on, whether there are children, pets or allergies, and whether it is a home or a business.

Urgent jobs: treat a job as urgent if it involves {BUSINESS['urgent_means']}. If it is urgent, tell the caller you're flagging it as urgent now and the owner will call them back very quickly.

Once you have the details, call the send_job_to_owner tool. Then tell the caller the team will call them back shortly to arrange a time, ask if there's anything else, and when the call is finished call end_call.

Other situations:
- If the caller wants a human, take their details and promise a quick callback.
- If they're outside the areas covered, say so politely, and still send the details to the owner.
- If an existing customer has a question, take a message and send it with send_job_to_owner.
- If it's a sales or spam call, say goodbye politely and call end_call without sending anything.

Hard rules. Never:
- give advice on using poisons, traps or chemicals
- promise exact prices or arrival times
- make up information about the business that isn't listed above
- argue with an angry caller; stay calm and kind"""

TOOLS = [
    {
        "name": "send_job_to_owner",
        "description": "Text the job details to the business owner. Call this once you have the caller's details, or to pass on a message.",
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Caller's name"},
                "phone": {"type": "string", "description": "Best number to call them back on"},
                "address": {"type": "string", "description": "Address and postcode"},
                "pest": {"type": "string", "description": "What the pest is and where it is"},
                "notes": {"type": "string", "description": "How long it's been going on, kids, pets, allergies, home or business, any message"},
                "urgent": {"type": "boolean", "description": "True if the job counts as urgent"},
            },
            "required": ["name", "phone", "pest", "urgent"],
        },
    },
    {
        "name": "end_call",
        "description": "Hang up after saying goodbye, when the conversation is finished.",
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": {}},
    },
]

claude = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY
app = FastAPI()


# ---------- Owner alerts ----------

def format_job(job: dict, caller_number: str) -> str:
    head = "URGENT job from Hush" if job.get("urgent") else "New job from Hush"
    lines = [
        head,
        f"Name: {job.get('name', '-')}",
        f"Call back: {job.get('phone') or caller_number or '-'}",
        f"Address: {job.get('address', '-')}",
        f"Pest: {job.get('pest', '-')}",
    ]
    if job.get("notes"):
        lines.append(f"Notes: {job['notes']}")
    return "\n".join(lines)


def send_sms(body: str) -> str:
    """Text the owner. Returns a short status for Claude."""
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_NUMBER and OWNER_PHONE):
        log.warning("SMS not configured, would have sent:\n%s", body)
        return "Saved (texting isn't set up yet, so it was logged instead)."
    from twilio.rest import Client

    Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).messages.create(
        to=OWNER_PHONE, from_=TWILIO_NUMBER, body=body
    )
    return "Sent to the owner."


def validate_job(job: dict) -> str | None:
    """Tool inputs stream in as they are written, so check them before acting."""
    for key in ("name", "phone", "pest"):
        if not isinstance(job.get(key), str):
            return f"Missing {key}."
    if not isinstance(job.get("urgent"), bool):
        return "Missing urgent."
    return None


# ---------- Twilio webhook ----------

@app.get("/")
async def health():
    return {"ok": True, "service": "hush"}


@app.post("/voice")
async def voice(request: Request):
    """Twilio calls this when the phone rings. We hand the call to ConversationRelay."""
    if TWILIO_AUTH_TOKEN and PUBLIC_URL:
        from twilio.request_validator import RequestValidator

        form = dict(await request.form())
        signature = request.headers.get("X-Twilio-Signature", "")
        if not RequestValidator(TWILIO_AUTH_TOKEN).validate(f"{PUBLIC_URL}/voice", form, signature):
            log.warning("Rejected /voice request with a bad Twilio signature")
            return Response(status_code=403)

    base = PUBLIC_URL or f"https://{request.headers.get('host')}"
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f"<ConversationRelay url={quoteattr(ws_url)} welcomeGreeting={quoteattr(WELCOME)} language=\"en-GB\" />"
        "</Connect></Response>"
    )
    return Response(content=twiml, media_type="text/xml")


# ---------- The live call ----------

class Call:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.caller = ""
        self.messages: list[dict] = []
        self.task: asyncio.Task | None = None
        self.hang_up = False
        self.tools_task: asyncio.Future | None = None

    async def say(self, token: str, last: bool):
        await self.ws.send_json({"type": "text", "token": token, "last": last})

    async def respond(self):
        """Ask Claude for the next reply, speak it, and run any tools it calls."""
        for _ in range(5):  # a reply never needs more than a few tool rounds
            try:
                async with claude.messages.stream(
                    model=MODEL,
                    max_tokens=1024,
                    system=SYSTEM_PROMPT + f"\n\nThe caller is ringing from {self.caller or 'an unknown number'}.",
                    thinking={"type": "disabled"},
                    output_config={"effort": "low"},
                    tools=TOOLS,
                    messages=self.messages,
                ) as stream:
                    async for text in stream.text_stream:
                        await self.say(text, last=False)
                    reply = await stream.get_final_message()
            except ValueError:
                log.warning("Claude sent a tool call that couldn't be read; retrying")
                continue
            await self.say("", last=True)

            tool_uses = [b for b in reply.content if b.type == "tool_use"]
            if reply.stop_reason in ("refusal", "max_tokens") or not tool_uses:
                text = "".join(b.text for b in reply.content if b.type == "text")
                if text:
                    self.messages.append({"role": "assistant", "content": text})
                break

            # Shielded so a caller interrupting can't leave a tool half-done
            # (that would break the conversation history or lose a job).
            self.tools_task = asyncio.ensure_future(self.run_tools(reply, tool_uses))
            await asyncio.shield(self.tools_task)

            if self.hang_up:
                await asyncio.sleep(4)  # let the goodbye finish playing
                await self.ws.send_json({"type": "end"})
                break

    async def run_tools(self, reply, tool_uses):
        results = []
        for tool in tool_uses:
            if tool.name == "send_job_to_owner":
                problem = validate_job(tool.input)
                if problem:
                    results.append({"type": "tool_result", "tool_use_id": tool.id, "content": problem, "is_error": True})
                    continue
                body = format_job(tool.input, self.caller)
                log.info("Job:\n%s", body)
                try:
                    status = await asyncio.to_thread(send_sms, body)
                except Exception as e:  # don't let a texting failure drop the call
                    log.exception("SMS failed")
                    status = f"Couldn't text the owner ({type(e).__name__}), but the details were saved."
                results.append({"type": "tool_result", "tool_use_id": tool.id, "content": status})
            elif tool.name == "end_call":
                self.hang_up = True
                results.append({"type": "tool_result", "tool_use_id": tool.id, "content": "Ending the call."})
            else:
                results.append({"type": "tool_result", "tool_use_id": tool.id, "content": "Unknown tool.", "is_error": True})
        self.messages.append({"role": "assistant", "content": reply.content})
        self.messages.append({"role": "user", "content": results})

    def caller_said(self, text: str):
        # Keep the conversation strictly alternating: merge into a trailing user turn
        # that holds only text, otherwise start a new one.
        last = self.messages[-1] if self.messages else {"role": "assistant"}
        if last["role"] == "user" and isinstance(last["content"], str):
            last["content"] += " " + text
        elif last["role"] == "user":
            last["content"].append({"type": "text", "text": text})
        else:
            self.messages.append({"role": "user", "content": text})

    def start_reply(self):
        self.task = asyncio.create_task(self.respond())
        self.task.add_done_callback(_log_errors)


def _log_errors(task: asyncio.Task):
    if not task.cancelled() and task.exception():
        log.error("Reply failed", exc_info=task.exception())


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    call = Call(ws)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")
            if kind == "setup":
                call.caller = msg.get("from", "")
                log.info("Call started from %s (%s)", call.caller, msg.get("callSid"))
            elif kind == "prompt" and msg.get("last", True):
                text = msg.get("voicePrompt", "").strip()
                if not text:
                    continue
                log.info("Caller: %s", text)
                if call.task and not call.task.done():
                    call.task.cancel()  # they spoke again before we finished; answer the latest
                    try:
                        await call.task
                    except (asyncio.CancelledError, Exception):
                        pass
                if call.tools_task and not call.tools_task.done():
                    await call.tools_task  # finish any job being sent first
                call.caller_said(text)
                call.start_reply()
            elif kind == "interrupt":
                # Caller talked over Hush. Stop generating; Twilio already stopped the audio.
                if call.task and not call.task.done():
                    call.task.cancel()
            elif kind == "error":
                log.error("Twilio error: %s", msg.get("description"))
    except WebSocketDisconnect:
        log.info("Call ended")
    finally:
        if call.task and not call.task.done():
            call.task.cancel()
