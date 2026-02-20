"""
Telegram Group Scanner → Slack DM Digest

Reads recent messages from configured Telegram groups, uses GPT-4o-mini to
identify founder intros & project announcements, and sends a digest to Slack.
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone

from openai import OpenAI
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from telethon import TelegramClient
from telethon.sessions import StringSession

# --- Config ---
TELEGRAM_API_ID = 33516003
TELEGRAM_API_HASH = "e55fb2c7bcad95849e4520ca89bdd72b"
TELEGRAM_SESSION = "1AZWarzsBu0I8ILcKvEtUX7RmFHA42OGBzj7lK_QHb4ed6EzTfl7_d8SbXvE4RuigY5CtuXOFUixaHWGfB8j-MosgsWALkYaZMd7ZD17BILgHefiutAbImCYK3M_LhXK-fkxiwRbrugCDW8u5ssZ9qmdo6mq6zvhNYH57kZjLkTikGxn1B3YG255blRbjYtujBiKY1KdT5HV9RdBUTdooghqOoFvMP_yBV9d6uaibE3qRrlEEEf8iqTNrJkXCUxgmKTgd2LfLPGtyMZgU9n16PHMS-LOqHNjnmQ3xnigY6FsAxGDTBej8mBYT5PFNrx6VGfN4EsnP_ZO8KDn28oCnocQ9eSmKF3M="
OPENAI_API_KEY = "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"
SLACK_BOT_TOKEN = "xoxb-5736340339410-9698047778609-dqUa7c0cxcQyM7zdz2bcUPnm"
SLACK_CHANNEL = "C09LV45H77D"

with open("config.json") as f:
    CONFIG = json.load(f)

GROUPS = CONFIG["groups"]
SCAN_HOURS = CONFIG.get("scan_hours", 48)
MODEL = CONFIG.get("model", "gpt-4o-mini")
IGNORE_USERNAMES = {u.lower() for u in CONFIG.get("ignore_usernames", [])}

GPT_SYSTEM_PROMPT = """You are an analyst scanning Telegram group messages for a venture investor.

From the messages provided, identify ONLY items that match these categories:
1. Founder introductions (someone introducing themselves and their project)
2. Project announcements (new products, features, platforms)
3. Funding news (raises, rounds, grants)
4. Product launches or major milestones

EXCLUDE these types of messages — they are NOT relevant:
- Freelancers or consultants looking for work or offering their personal services
- Agencies selling custom/bespoke services (e.g., "we'll build your app", "hire us for marketing")
- Generic service pitches without a distinct product

DO INCLUDE B2B startups that have a scalable product, even if they mention client results or case studies. The key distinction: a startup has a product that clients use, while an agency/freelancer sells custom labor.

For each relevant item, return a concise one-line summary in this format:
• @sender: summary of what was announced/introduced
  → MSG_ID:{message_id}

If nothing notable is found, respond with exactly: Nothing notable today

Be selective — ignore casual chat, questions, memes, support requests, and general discussion."""


def extract_urls(text):
    """Extract all URLs from text using regex."""
    return re.findall(r'https?://[^\s<>\"\'\)]+', text)


STRUCTURED_LEADS_PROMPT = """You are given a GPT analysis of Telegram group messages and the raw messages themselves.

Extract each identified lead into structured JSON. Return a JSON array where each element has:
- "sender_username": the @username of the sender (without @)
- "summary": the one-line summary from the analysis
- "message_id": the numeric message ID
- "relevant_message_text": the relevant portion of the raw message (max 500 chars)

Only include leads that appear in the analysis (not messages marked as irrelevant).
If the analysis says "Nothing notable today", return an empty array: []

Return ONLY valid JSON, no markdown fences or extra text."""


def extract_structured_leads(analysis_text, messages_text, group_name):
    """Use GPT-4o-mini to extract structured lead data from the analysis."""
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": STRUCTURED_LEADS_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Group: {group_name}\n\n"
                    f"Analysis:\n{analysis_text}\n\n"
                    f"Raw messages:\n{messages_text}"
                ),
            },
        ],
        temperature=0.1,
        max_tokens=2000,
    )
    raw = response.choices[0].message.content.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"  ⚠️  Failed to parse structured leads for {group_name}")
        return []


def build_telegram_link(group, msg_id):
    """Build a deep link to a Telegram message."""
    group_id = group["id"]
    username = group.get("username")
    if username:
        return f"https://t.me/{username}/{msg_id}"
    # Private group: strip the -100 prefix
    raw_id = str(group_id)
    if raw_id.startswith("-100"):
        raw_id = raw_id[4:]
    return f"https://t.me/c/{raw_id}/{msg_id}"


async def fetch_messages(client, group):
    """Fetch messages from a group for the last SCAN_HOURS."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SCAN_HOURS)
    messages = []
    try:
        entity = await client.get_entity(group["id"])
        async for msg in client.iter_messages(entity, offset_date=datetime.now(timezone.utc), limit=None):
            if msg.date < cutoff:
                break
            sender_name = "Unknown"
            if msg.sender:
                sender_name = getattr(msg.sender, "username", None) or getattr(
                    msg.sender, "first_name", "Unknown"
                )
            if sender_name.lower() in IGNORE_USERNAMES:
                continue
            if msg.text and len(msg.text) >= 50:
                messages.append(
                    {
                        "id": msg.id,
                        "sender": sender_name,
                        "date": msg.date.strftime("%Y-%m-%d %H:%M"),
                        "text": msg.text[:1000],  # truncate long messages
                    }
                )
    except Exception as e:
        print(f"  ⚠️  Error fetching {group['name']}: {e}")
    return messages


def format_messages_for_gpt(messages):
    """Format messages into a text block for GPT analysis."""
    lines = []
    for m in messages:
        lines.append(f"[{m['date']}] @{m['sender']} (MSG_ID:{m['id']}): {m['text']}")
    return "\n".join(lines)


def analyze_with_gpt(group_name, messages_text):
    """Send messages to GPT-4o-mini for analysis."""
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": GPT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Group: {group_name}\n\nMessages:\n{messages_text}",
            },
        ],
        temperature=0.3,
        max_tokens=1000,
    )
    return response.choices[0].message.content.strip()


def send_to_slack(digest_text):
    """Send the digest to Slack."""
    client = WebClient(token=SLACK_BOT_TOKEN)
    try:
        client.chat_postMessage(channel=SLACK_CHANNEL, text=digest_text, unfurl_links=False, unfurl_media=False)
        print("✅ Digest sent to Slack")
    except SlackApiError as e:
        print(f"❌ Slack error: {e.response['error']}")
        raise


def send_error_to_slack(error_msg):
    """Alert on Slack if something goes wrong."""
    try:
        client = WebClient(token=SLACK_BOT_TOKEN)
        client.chat_postMessage(
            channel=SLACK_CHANNEL,
            text=f"⚠️ Telegram Scanner Error:\n```{error_msg}```",
        )
    except Exception:
        pass  # best-effort


async def main():
    print(f"🔍 Starting Telegram scan — looking back {SCAN_HOURS}h")

    telegram = TelegramClient(
        StringSession(TELEGRAM_SESSION), TELEGRAM_API_ID, TELEGRAM_API_HASH
    )
    await telegram.connect()

    if not await telegram.is_user_authorized():
        send_error_to_slack("Telegram session expired or invalid. Re-run generate_session.py.")
        print("❌ Not authorized. Session may have expired.")
        sys.exit(1)

    today = datetime.now(timezone.utc).strftime("%b %d, %Y")
    scan_timestamp = datetime.now(timezone.utc).isoformat()
    all_leads = []

    for group in GROUPS:
        print(f"\n  Scanning: {group['name']}")
        messages = await fetch_messages(telegram, group)
        print(f"   Found {len(messages)} messages")

        if not messages:
            continue

        messages_text = format_messages_for_gpt(messages)
        analysis = analyze_with_gpt(group["name"], messages_text)

        # Build a lookup of raw messages by ID for URL extraction
        messages_by_id = {m["id"]: m for m in messages}

        # Extract structured leads before replacing MSG_IDs with links
        if "nothing notable" not in analysis.lower():
            structured = extract_structured_leads(analysis, messages_text, group["name"])
            for lead in structured:
                msg_id = lead.get("message_id")
                raw_msg = messages_by_id.get(int(msg_id)) if msg_id else None
                raw_text = raw_msg["text"] if raw_msg else lead.get("relevant_message_text", "")
                all_leads.append({
                    "id": f"{group['name']}_{msg_id}",
                    "group_name": group["name"],
                    "sender_username": lead.get("sender_username", "Unknown"),
                    "message_text": raw_text,
                    "summary": lead.get("summary", ""),
                    "telegram_link": build_telegram_link(group, msg_id) if msg_id else "",
                    "urls_in_message": extract_urls(raw_text),
                })

    await telegram.disconnect()

    # Write leads.json for the enricher
    leads_data = {"scan_date": scan_timestamp, "leads": all_leads}
    with open("leads.json", "w") as f:
        json.dump(leads_data, f, indent=2)
    print(f"\n📝 Wrote {len(all_leads)} leads to leads.json")

    if not all_leads:
        print("No notable findings — skipping digest")
        send_to_slack(f"Telegram Digest — {today}\n\nNo notable findings across {len(GROUPS)} groups.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        send_error_to_slack(str(e))
        sys.exit(1)
