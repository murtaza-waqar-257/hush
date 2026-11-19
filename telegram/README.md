# Hush Enquiry Catcher: Telegram demo (v1)

A Telegram bot that acts like Hush texting back a missed call for a made-up firm, **Northvale Pest Control**.
It asks about the pest, quotes from the price list, offers free slots, books the job and sends the owner an alert.
Costs £0: Telegram is free and the AI uses Google Gemini's free tier.

## Set up (about 15 minutes, once)

You need Python 3.10 or newer on your Mac. Check with `python3 --version` in Terminal.

**1. Make the bot in Telegram**
- Open Telegram and message **@BotFather**.
- Send `/newbot`, pick a name (e.g. `Northvale Pest Control`) and a username ending in `bot` (e.g. `northvale_hush_bot`).
- BotFather gives you a token like `123456789:ABC...`. Keep it secret.

**2. Get a free Gemini key**
- Go to https://aistudio.google.com/apikey, sign in with Google, and click **Create API key**.
- Note: on the free tier Google may use what you send to improve its products. Only use fake demo data.

**3. Install and add your keys**
```
cd telegram
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
open -e .env
```
Paste your Telegram token and Gemini key into `.env` and save.

**4. Run it and get your owner alerts**
```
python bot.py
```
- Open your bot in Telegram and send `/start`.
- In Terminal you'll see `Your chat id is 123456789`. Copy that number into `OWNER_CHAT_ID` in `.env`.
- Stop the bot with Ctrl+C and run `python bot.py` again. Now booking alerts come to you.

## Try it

Pretend you're a customer:
- "I've got wasps going in and out of my roof" → it should quote £95, ask your postcode, offer slots and book.
- "Mice in the kitchen, OL6" → £145, 3 visits.
- "Bed bugs in two bedrooms" → it should **not** book, and you get a "Needs you" alert.
- "We run a restaurant and have rats" → handed to the owner.
- "Can you do it for £50?" → it should stick to the price list.
- A Leeds postcode (LS6) → it should say the area isn't covered.

Commands: `/start` new chat · `/reset` clear the chat · `/bookings` (owner only) latest bookings.

## Change the business

Everything about the firm is in `business.json`: name, areas, postcodes, opening hours, time slots, prices
and what always goes to the owner. Edit it and restart the bot. Bookings are saved in `bookings.json`.

## Known limits of v1

- Runs only while your laptop is on and `python bot.py` is running.
- Chats are forgotten when you restart (bookings are kept).
- Real UK customers won't use Telegram. This is the demo and the "brain"; later the customer side moves to SMS or WhatsApp.
- If you hit Gemini's free limit you'll see an error message; wait a minute and try again.
