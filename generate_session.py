"""
One-time local script to generate a Telegram session string.
Run this locally, then store the output as the SESSION_STRING GitHub secret.

Usage:
    pip install telethon
    python generate_session.py
"""

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = input("Enter your API_ID: ").strip()
API_HASH = input("Enter your API_HASH: ").strip()


async def main():
    client = TelegramClient(StringSession(), int(API_ID), API_HASH)
    await client.start()
    session_string = client.session.save()
    print("\n✅ Your session string (store as SESSION_STRING secret):\n")
    print(session_string)
    print()
    await client.disconnect()


asyncio.run(main())
