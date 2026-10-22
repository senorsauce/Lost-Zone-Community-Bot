import logging
import discord
from discord.ext import commands

from config import TOKEN, DELETE_DELAY_SECONDS
from radio import RadioCog
from dayz import DayzCog
from announcement import AnnouncementCog
from ticketing import TicketingCog
from suggestions import SuggestionCog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ZVTBOT")


intents = discord.Intents.default()
intents.voice_states = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}")

    if not hasattr(bot, "_commands_synced"):
        try:
            for guild in bot.guilds:
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                print(f"Synced {len(synced)} slash command(s) to {guild.name} ({guild.id})")
            bot._commands_synced = True
        except Exception as error:
            print(f"Failed to sync slash commands: {type(error).__name__}: {error}")


async def main() -> None:
    await bot.add_cog(RadioCog(bot, DELETE_DELAY_SECONDS), override=True)
    await bot.add_cog(DayzCog(bot))
    await bot.add_cog(AnnouncementCog(bot))
    await bot.add_cog(TicketingCog(bot))
    await bot.add_cog(SuggestionCog(bot))
    await bot.start(TOKEN)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
