"""
One-time utility to list all Telegram groups/channels the account is in.
Compares against config.json to show which are new vs already tracked.

Usage:
    source .venv/bin/activate
    pip install python-dotenv
    python list_groups.py
"""

import asyncio
import json
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat

load_dotenv()

SESSION = os.environ["TELEGRAM_SESSION"]
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]

# Load existing config for comparison
with open("config.json") as f:
    config = json.load(f)
existing_ids = {g["id"] for g in config["groups"]}


async def main():
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        print("Session is not authorized. Re-run generate_session.py.")
        return

    print("\nFetching all dialogs...\n")

    groups = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, Channel) and entity.megagroup:
            # Telethon Channel IDs need the -100 prefix for our config format
            full_id = int(f"-100{entity.id}")
            groups.append({
                "id": full_id,
                "name": dialog.name,
                "members": getattr(entity, "participants_count", None),
                "username": getattr(entity, "username", None),
            })
        elif isinstance(entity, Chat):
            groups.append({
                "id": -entity.id,
                "name": dialog.name,
                "members": getattr(entity, "participants_count", None),
                "username": None,
            })

    # Separate into tracked and new
    tracked = [g for g in groups if g["id"] in existing_ids]
    new = [g for g in groups if g["id"] not in existing_ids]

    print(f"=== ALREADY IN CONFIG ({len(tracked)}) ===")
    for g in sorted(tracked, key=lambda x: x["name"]):
        members = f" ({g['members']} members)" if g["members"] else ""
        print(f"  {g['name']}{members} -> ID: {g['id']}")

    print(f"\n=== NEW GROUPS ({len(new)}) ===")
    for g in sorted(new, key=lambda x: x["name"]):
        members = f" ({g['members']} members)" if g["members"] else ""
        username = f" @{g['username']}" if g["username"] else ""
        print(f"  {g['name']}{members}{username} -> ID: {g['id']}")

    await client.disconnect()


asyncio.run(main())
