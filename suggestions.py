import asyncio
import logging
import os
import re

import discord
from discord import app_commands
from discord.ext import commands


logger = logging.getLogger(__name__)

SUGGESTION_CHANNEL_ID = int(
    os.getenv("SUGGESTION_CHANNEL_ID", "1531432683702456411")
)
ALLOWED_REACTIONS = {"✅", "❌"}
SUGGESTION_NUMBER_PATTERN = re.compile(r"^#(\d{2,})$")
LEGACY_SUGGESTION_NUMBER_PATTERN = re.compile(r"^Suggestion #(\d+)$")


class SuggestionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._suggestion_number = 0
        self._counter_initialized = False
        self._submission_lock = asyncio.Lock()

    async def _get_suggestion_channel(self) -> discord.TextChannel | None:
        channel = self.bot.get_channel(SUGGESTION_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(SUGGESTION_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.error(
                    "Could not fetch suggestion channel %s: %s",
                    SUGGESTION_CHANNEL_ID,
                    exc,
                )
                return None
        if not isinstance(channel, discord.TextChannel):
            logger.error("Suggestion destination %s is not a text channel.", SUGGESTION_CHANNEL_ID)
            return None
        return channel

    async def _next_suggestion_number(self, channel: discord.TextChannel) -> int:
        if not self._counter_initialized:
            highest_number = 0
            async for message in channel.history(limit=None, oldest_first=False):
                if message.author != self.bot.user or not message.embeds:
                    continue
                match = self._suggestion_number_match(message.embeds[0].title or "")
                if match:
                    highest_number = max(highest_number, int(match.group(1)))
            self._suggestion_number = highest_number
            self._counter_initialized = True

        self._suggestion_number += 1
        return self._suggestion_number

    @staticmethod
    def _suggestion_number_match(title: str) -> re.Match[str] | None:
        return (
            SUGGESTION_NUMBER_PATTERN.fullmatch(title)
            or LEGACY_SUGGESTION_NUMBER_PATTERN.fullmatch(title)
        )

    def _is_suggestion_message(self, message: discord.Message) -> bool:
        return bool(
            message.author == self.bot.user
            and message.embeds
            and self._suggestion_number_match(message.embeds[0].title or "")
        )

    @app_commands.command(
        name="suggest",
        description="Submit a suggestion for the server",
    )
    @app_commands.guild_only()
    @app_commands.describe(suggestion="Your suggestion")
    async def suggest(self, interaction: discord.Interaction, suggestion: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.channel_id != SUGGESTION_CHANNEL_ID:
            await interaction.followup.send(
                f"Run /suggest in <#{SUGGESTION_CHANNEL_ID}>.", ephemeral=True
            )
            return
        channel = await self._get_suggestion_channel()
        if channel is None:
            await interaction.followup.send(
                "The suggestion channel is unavailable. Please contact Staff.",
                ephemeral=True,
            )
            return

        if interaction.guild_id != channel.guild.id:
            await interaction.followup.send(
                "Suggestions can only be submitted from this server.", ephemeral=True
            )
            return

        async with self._submission_lock:
            try:
                suggestion_number = await self._next_suggestion_number(channel)
                embed = discord.Embed(
                    title=f"#{suggestion_number:02d}",
                    description=suggestion,
                    colour=discord.Colour(0x3C3CE8),
                )
                message = await channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Could not post suggestion from user %s.", interaction.user.id
                )
                await interaction.followup.send(
                    "Your suggestion could not be posted. Please try again.",
                    ephemeral=True,
                )
                return

            failed_reactions = []
            for emoji in ("✅", "❌"):
                try:
                    await message.add_reaction(emoji)
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Could not add %s reaction to suggestion %s.",
                        emoji,
                        message.id,
                    )
                    failed_reactions.append(emoji)

        reply = f"Your suggestion was posted as #{suggestion_number:02d}."
        if failed_reactions:
            reply += " The bot could not add all voting reactions; check its Add Reactions permission."
        await interaction.followup.send(reply, ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if payload.channel_id != SUGGESTION_CHANNEL_ID:
            return
        if payload.user_id == getattr(self.bot.user, "id", None):
            return
        if str(payload.emoji) in ALLOWED_REACTIONS:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Could not fetch suggestion channel for reaction: %s", exc)
                return
        if not isinstance(channel, discord.TextChannel):
            return

        message = payload.cached_message
        if message is None:
            try:
                message = await channel.fetch_message(payload.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Could not fetch message for reaction filtering: %s", exc)
                return
        if not self._is_suggestion_message(message):
            return

        member = payload.member
        if member is None:
            member = channel.guild.get_member(payload.user_id)
            if member is None:
                try:
                    member = await channel.guild.fetch_member(payload.user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning("Could not resolve reaction user: %s", exc)
                    return
        try:
            await message.remove_reaction(payload.emoji, member)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.error(
                "Could not remove disallowed reaction %s from suggestion %s: %s",
                payload.emoji,
                payload.message_id,
                exc,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SuggestionCog(bot))