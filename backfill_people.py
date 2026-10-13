"""
Historical backfill for the people database.

Fetches all qualifying messages from the last LOOKBACK_DAYS (default 180) from
every group in config.json and writes them to people.db via people_db.

Run once to seed the database, then scanner.py keeps it up to date on each run.

Usage:
    python backfill_people.py
    LOOKBACK_DAYS=90 python backfill_people.py   # shorter window
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient
from telethon.sessions import StringSession

from people_db import init_db, upsert_message, stats

TELEGRAM_API_ID  = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH     = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_SESSION      = os.environ["TELEGRAM_SESSION"]

with open("config.json") as f:
    CONFIG = json.load(f)

GROUPS           = CONFIG["groups"]
IGNORE_USERNAMES = {u.lower() for u in CONFIG.get("ignore_usernames", [])}
LOOKBACK_DAYS    = int(os.environ.get("LOOKBACK_DAYS", "180"))
MIN_LENGTH       = 50


def _tg_link(group: dict, msg_id: int) -> str:
    username = group.get("username")
    if username:
        return f"https://t.me/{username}/{msg_id}"
    raw = str(group["id"])
    if raw.startswith("-100"):
        raw = raw[4:]
    return f"https://t.me/c/{raw}/{msg_id}"


async def backfill() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    print(f"📅  Backfill: messages since {cutoff.strftime('%Y-%m-%d')}  ({LOOKBACK_DAYS} days)")
    print(f"📂  Groups: {len(GROUPS)}\n")

    conn = init_db()

    tg = TelegramClient(StringSession(TELEGRAM_SESSION), TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await tg.connect()

    if not await tg.is_user_authorized():
        print("❌  Telegram session expired — re-run generate_session.py")
        sys.exit(1)

    total_new = 0

    for group in GROUPS:
        print(f"  📂  {group['name']}")
        group_total = 0
        group_new   = 0

        try:
            entity = await tg.get_entity(group["id"])
            async for msg in tg.iter_messages(
                entity, offset_date=datetime.now(timezone.utc), limit=None
            ):
                if msg.date.replace(tzinfo=timezone.utc) < cutoff if msg.date.tzinfo is None else msg.date < cutoff:
                    break
                if not msg.text or len(msg.text) < MIN_LENGTH:
                    continue

                sender = "Unknown"
                if msg.sender:
                    sender = (
                        getattr(msg.sender, "username", None)
                        or getattr(msg.sender, "first_name", "Unknown")
                    )

                if not sender or sender == "Unknown" or sender.lower() in IGNORE_USERNAMES:
                    continue

                group_total += 1
                sent_at = msg.date.strftime("%Y-%m-%dT%H:%M:%S")

                added = upsert_message(
                    conn,
                    username=sender,
                    group_id=group["id"],
                    group_name=group["name"],
                    message_id=msg.id,
                    text=msg.text[:2000],
                    telegram_link=_tg_link(group, msg.id),
                    sent_at=sent_at,
                )
                if added:
                    group_new += 1

        except Exception as exc:
            print(f"    ⚠️  Error: {exc}")
        finally:
            conn.commit()

        total_new += group_new
        print(f"      {group_total:>5} qualifying messages  |  {group_new:>5} new\n")

    await tg.disconnect()
    conn.close()

    s = stats()
    print(f"✅  Done.  {total_new} messages added this run.")
    print(f"📊  Database totals: {s['people']} people  ·  {s['messages']} messages  ·  {s['groups']} groups")


if __name__ == "__main__":
    asyncio.run(backfill())
