"""
Telegram Group Scanner → Slack DM Digest

Reads recent messages from configured Telegram groups, uses GPT-4o-mini to
identify founder intros & project announcements, and sends a digest to Slack.
"""

import asyncio
import json
import os
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
SCAN_HOURS = CONFIG.get("scan_hours", 24)
MODEL = CONFIG.get("model", "gpt-4o-mini")

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


def replace_msg_id_links(analysis_text, group):
    """Replace MSG_ID:{id} placeholders with actual Telegram deep links."""
    import re

    def replacer(match):
        msg_id = match.group(1)
        return build_telegram_link(group, msg_id)

    return re.sub(r"MSG_ID:(\d+)", replacer, analysis_text)


def send_to_slack(digest_text):
    """Send the digest to Slack."""
    client = WebClient(token=SLACK_BOT_TOKEN)
    try:
        client.chat_postMessage(channel=SLACK_CHANNEL, text=digest_text)
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
    digest_sections = [f"Telegram Digest -- {today}\n"]
    skipped_count = 0

    for group in GROUPS:
        print(f"\n  Scanning: {group['name']}")
        messages = await fetch_messages(telegram, group)
        print(f"   Found {len(messages)} messages")

        if not messages:
            skipped_count += 1
            continue

        messages_text = format_messages_for_gpt(messages)
        analysis = analyze_with_gpt(group["name"], messages_text)
        analysis = replace_msg_id_links(analysis, group)

        if "nothing notable" in analysis.lower():
            skipped_count += 1
            continue

        digest_sections.append(f"{group['name']}\n{analysis}\n")

    await telegram.disconnect()

    if skipped_count > 0:
        channel_word = "channel" if skipped_count == 1 else "channels"
        digest_sections.append(f"Nothing relevant in {skipped_count} other {channel_word}.")

    digest = "\n".join(digest_sections)
    print("\n--- Digest ---")
    print(digest)

    if len(digest_sections) > 2:
        # There are actual findings (header + at least one group + skipped summary)
        send_to_slack(digest)
    else:
        print("No notable findings today -- skipping detailed digest")
        send_to_slack(f"Telegram Digest -- {today}\n\nNo notable findings today across {len(GROUPS)} groups.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        send_error_to_slack(str(e))
        sys.exit(1)
