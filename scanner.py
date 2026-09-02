"""
Telegram Group Scanner → Slack DM Digest

Reads recent messages from configured Telegram groups, uses GPT-4o-mini to
identify founder intros & project announcements, and sends a digest to Slack.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from openai import OpenAI
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from telethon import TelegramClient
from telethon.sessions import StringSession

# --- Config (from GitHub Actions secrets / environment) ---
TELEGRAM_API_ID = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_SESSION = os.environ["TELEGRAM_SESSION"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "C09LV45H77D")

with open("config.json") as f:
    CONFIG = json.load(f)

GROUPS = CONFIG["groups"]
SCAN_HOURS = CONFIG.get("scan_hours", 48)
MODEL = CONFIG.get("model", "gpt-4o-mini")
IGNORE_USERNAMES = {u.lower() for u in CONFIG.get("ignore_usernames", [])}

SEEN_SENDERS_FILE = "seen_senders.json"
DEDUP_DAYS = 7

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
- Price discussion, market speculation, trading signals, and token pumps/dumps
- In-person events of any kind: meetups, conferences, side events, happy hours, dinners, workshops, hackathons, summits, co-working days, networking nights, or announcements/recaps of physical gatherings (including event invites, RSVPs, photos, and thank-you posts)

DO INCLUDE B2B startups that have a scalable product, even if they mention client results or case studies. The key distinction: a startup has a product that clients use, while an agency/freelancer sells custom labor.

For each relevant item, return a concise one-line summary in this format:
• @sender: summary of what was announced/introduced
  → MSG_ID:{message_id}

If nothing notable is found, respond with exactly: Nothing notable today

Be selective — ignore casual chat, questions, memes, support requests, and general discussion."""


STRUCTURED_LEADS_PROMPT = """You are given a GPT analysis of Telegram group messages and the raw messages themselves.

Extract each identified lead into structured JSON. Return a JSON array where each element has:
- "sender_username": the @username of the sender (without @)
- "summary": the one-line summary from the analysis
- "message_id": the numeric message ID
- "relevant_message_text": the relevant portion of the raw message (max 500 chars)

Only include leads that appear in the analysis (not messages marked as irrelevant).
If the analysis says "Nothing notable today", return an empty array: []

Return ONLY valid JSON, no markdown fences or extra text."""


def load_seen_senders():
    """Load the seen senders dict from disk. Returns {username_lower: date_str}."""
    try:
        with open(SEEN_SENDERS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen_senders(seen: dict, new_senders: list[str], today_str: str):
    """Purge entries older than DEDUP_DAYS, add new senders, write to disk."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)).strftime("%Y-%m-%d")
    # Purge stale entries
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    # Add newly surfaced senders
    for username in new_senders:
        seen[username.lower()] = today_str
    with open(SEEN_SENDERS_FILE, "w") as f:
        json.dump(seen, f, indent=2, sort_keys=True)
    return seen


def is_seen_this_week(username: str, seen: dict) -> bool:
    """Return True if this sender was already surfaced within DEDUP_DAYS."""
    date_str = seen.get(username.lower())
    if not date_str:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)).strftime("%Y-%m-%d")
    return date_str >= cutoff


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
                        "text": msg.text[:1000],
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


def send_digest(leads, skipped_count, today):
    """Send the plain-text digest to Slack."""
    client = WebClient(token=SLACK_BOT_TOKEN)

    if not leads:
        text = f"Telegram Digest — {today}\n\nNo notable findings across {len(GROUPS)} groups."
        if skipped_count:
            text += f"\n({skipped_count} sender(s) skipped — already surfaced this week)"
    else:
        lines = [f"Telegram Digest — {today}", f"{len(leads)} lead(s) across {len(GROUPS)} groups\n"]
        if skipped_count:
            lines.append(f"_{skipped_count} sender(s) skipped — already surfaced this week_\n")
        for lead in leads:
            sender = lead.get("sender_username", "Unknown")
            summary = lead.get("summary", "")
            group = lead.get("group_name", "")
            tg_link = lead.get("telegram_link", "")
            lines.append(f"• @{sender}: {summary}")
            lines.append(f"  {group} | {tg_link}")
            lines.append("")
        text = "\n".join(lines).strip()

    try:
        client.chat_postMessage(channel=SLACK_CHANNEL, text=text, unfurl_links=False, unfurl_media=False)
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
        pass


async def main():
    print(f"🔍 Starting Telegram scan — looking back {SCAN_HOURS}h")

    seen_senders = load_seen_senders()
    print(f"   Loaded {len(seen_senders)} seen sender(s) from the last {DEDUP_DAYS} days")

    telegram = TelegramClient(
        StringSession(TELEGRAM_SESSION), TELEGRAM_API_ID, TELEGRAM_API_HASH
    )
    await telegram.connect()

    if not await telegram.is_user_authorized():
        send_error_to_slack("Telegram session expired or invalid. Re-run generate_session.py.")
        print("❌ Not authorized. Session may have expired.")
        sys.exit(1)

    today = datetime.now(timezone.utc).strftime("%b %d, %Y")
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    scan_timestamp = datetime.now(timezone.utc).isoformat()
    all_leads = []
    skipped_count = 0

    for group in GROUPS:
        print(f"\n  Scanning: {group['name']}")
        messages = await fetch_messages(telegram, group)
        print(f"   Found {len(messages)} messages")

        if not messages:
            continue

        messages_text = format_messages_for_gpt(messages)
        analysis = analyze_with_gpt(group["name"], messages_text)
        print(f"   GPT: {analysis}")

        messages_by_id = {m["id"]: m for m in messages}

        if "nothing notable" not in analysis.lower():
            structured = extract_structured_leads(analysis, messages_text, group["name"])
            for lead in structured:
                msg_id = lead.get("message_id")
                sender = lead.get("sender_username", "Unknown")

                if is_seen_this_week(sender, seen_senders):
                    print(f"   ⏭️  Skipping @{sender} — already surfaced this week")
                    skipped_count += 1
                    continue

                raw_msg = messages_by_id.get(int(msg_id)) if msg_id else None
                raw_text = raw_msg["text"] if raw_msg else lead.get("relevant_message_text", "")
                all_leads.append({
                    "id": f"{group['name']}_{msg_id}",
                    "group_name": group["name"],
                    "sender_username": sender,
                    "message_text": raw_text,
                    "summary": lead.get("summary", ""),
                    "telegram_link": build_telegram_link(group, msg_id) if msg_id else "",
                })

    await telegram.disconnect()

    # Write leads.json
    leads_data = {"scan_date": scan_timestamp, "leads": all_leads}
    with open("leads.json", "w") as f:
        json.dump(leads_data, f, indent=2)
    print(f"\n📝 Wrote {len(all_leads)} leads to leads.json")

    # Update seen senders
    new_senders = [lead["sender_username"] for lead in all_leads]
    save_seen_senders(seen_senders, new_senders, today_str)
    print(f"📋 Updated seen_senders.json (+{len(new_senders)} new)")

    # Send digest
    send_digest(all_leads, skipped_count, today)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        send_error_to_slack(str(e))
        sys.exit(1)
