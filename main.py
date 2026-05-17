import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

load_dotenv()

GUILD_ID = 1433635828105744418
STAFF_ROLE_ID = 1433637723603865661
MANAGER_ROLE_ID = 1433750015972605992
TICKET_CHANNEL_ID = 1503164208555233391
TICKET_CATEGORY_ID = 1434220836914729144

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Error syncing commands: {e}")

async def main():
    async with bot:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        await bot.load_extension("cogs.vault")
        await bot.load_extension("cogs.tickets")
        await bot.start(os.getenv("DISCORD_TOKEN"))

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())