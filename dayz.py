import asyncio
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("ZVTBOT.dayz")


class DayzCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.radio_item_names = {"radio", "walkie-talkie", "walkie talkie"}

    def inventory_contains_radio(self, items: list[str] | None) -> bool:
        if not items:
            return False

        normalized = {item.lower().strip() for item in items if item and str(item).strip()}
        return any(name in normalized for name in self.radio_item_names)

    def extract_inventory_items(self, payload: dict[str, Any] | None) -> list[str]:
        if not isinstance(payload, dict):
            return []

        inventory = payload.get("inventory")
        if isinstance(inventory, list):
            extracted: list[str] = []
            for entry in inventory:
                if isinstance(entry, dict) and "name" in entry and isinstance(entry["name"], str):
                    extracted.append(entry["name"])
                elif isinstance(entry, str):
                    extracted.append(entry)
            if extracted:
                return extracted

        items = payload.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, str)]

        return []

    async def fetch_player_inventory(self, player_id: str) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        return None

    async def enforce_radio_inventory(self, member: discord.Member) -> None:
        if not isinstance(member, discord.Member):
            return

        if member.guild is None or member.voice is None or member.voice.channel is None:
            return

        if not self._is_radio_voice_channel(member.voice.channel):
            return

        player_id = str(getattr(member, "id", ""))
        payload = await self.fetch_player_inventory(player_id)
        items = self.extract_inventory_items(payload)
        has_radio = self.inventory_contains_radio(items)

        if has_radio:
            await member.edit(mute=False, deaf=False)
            return

        await member.edit(mute=True, deaf=True)

    def _is_radio_voice_channel(self, channel: discord.abc.GuildChannel | None) -> bool:
        if not isinstance(channel, discord.VoiceChannel):
            return False

        if channel.guild is None:
            return False

        try:
            from radio import RadioCog

            radio_cog = RadioCog.__new__(RadioCog)
            return radio_cog.is_radio_channel(channel)
        except Exception:
            return False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        print("DAYZ cog ready")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if before.channel == after.channel:
            return

        await self.enforce_radio_inventory(member)

    @app_commands.command(name="dayz-status", description="Check a DAYZ server status")
    async def dayz_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "DAYZ API integration is scaffolded and ready for the real endpoint.",
            ephemeral=True,
        )
