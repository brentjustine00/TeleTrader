import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

# Quick script to generate a reusable Telethon session string
# Run this locally once to get your session string, then add it to Render's environment variables.

async def main():
    api_id = int(input("Enter your Telegram API ID: ").strip())
    api_hash = input("Enter your Telegram API Hash: ").strip()
    
    print("\nStarting Telethon client...")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_str = client.session.save()
        print("\n" + "="*80)
        print("YOUR TELEGRAM SESSION STRING:")
        print(session_str)
        print("="*80)
        print("\nCopy the long string above. Keep it secret! Anyone with this string can access your Telegram account.")
        print("You can set this string as an environment variable or in config.json to log in on Render.")

if __name__ == "__main__":
    asyncio.run(main())
