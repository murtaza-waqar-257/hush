# Hush

An AI phone receptionist for pest control businesses. It answers the phone, takes the caller's details, and texts the job to the owner. Urgent jobs are marked **URGENT**.

## How it works

1. A customer rings your Twilio number.
2. Twilio turns what they say into text and sends it to this server.
3. The server asks Claude what to say back, and Twilio reads the reply out loud.
4. Once Hush has the details, it texts the job to the owner's mobile.

## What you need

- **Twilio account** (the free trial is fine for testing): your Account SID, Auth Token and Twilio phone number. They're all on the Twilio Console home page.
- **Claude API key** from console.anthropic.com. Calls cost pennies.
- **Render account** (free) to host the server.

## Setup

1. **Put the code on GitHub**, then on render.com choose **New → Blueprint** and pick the repo. Render reads `render.yaml` and sets everything up.
2. **Fill in the settings** Render asks for:
   - `ANTHROPIC_API_KEY`: your Claude API key
   - `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`: from Twilio
   - `TWILIO_NUMBER`: your Twilio number, like `+12055550123`
   - `OWNER_PHONE`: the mobile that gets the job texts, like `+447700900123`
   - `PUBLIC_URL`: your Render address, like `https://hush.onrender.com` (you'll see it after the first deploy, so add it then)
3. **Connect the phone number.** In Twilio, go to **Phone Numbers → Active numbers →** your number. Under **Voice → A call comes in**, choose **Webhook**, paste `https://YOUR-RENDER-ADDRESS/voice`, set it to **HTTP POST**, and save.
4. **Ring the number** from your verified mobile and talk to Hush.

## Changing the business details

Edit `business.json` to change the company name, services, areas, hours, prices and what counts as urgent.

## Trial limits

- Calls start with a short Twilio trial message.
- Job texts can only go to phone numbers you've verified in Twilio. Test by calling from your own verified mobile.
- Render's free plan goes to sleep after about 15 minutes with no use, so the first call after a quiet spell may fail. Open your Render address in a browser first to wake it up. A paid plan, about $7 a month, stays awake.

## Running it on a computer (optional)

```
pip install -r requirements.txt
cp .env.example .env    # then fill it in
uvicorn main:app --port 8000
```
