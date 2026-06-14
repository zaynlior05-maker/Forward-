"""
Run this script ONCE on your local machine to generate a SESSION_STRING.
Copy the printed string into your Railway environment variables.

    pip install telethon python-dotenv
    python generate_session.py
"""

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID   = int(input("Enter your API_ID: ").strip())
API_HASH = input("Enter your API_HASH: ").strip()


async def main():
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        session_string = client.session.save()
        print("\n" + "=" * 60)
        print("Your SESSION_STRING (copy this into Railway env vars):")
        print("=" * 60)
        print(session_string)
        print("=" * 60 + "\n")
        print("Keep this string private — it grants full access to your account.")


asyncio.run(main())
