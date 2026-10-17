import json
import logging
import os
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands


logger = logging.getLogger(__name__)


TICKET_TYPES = {
    "general_support": {
        "label": "General Support",
        "category_name": "General",
        "category_key": "category_id",
        "channel_prefix": "gen",
    }
}


def parse_ticket_guild_configs(raw: str) -> dict[str, dict[str, int]]:
    if not raw.strip():
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"TICKET_GUILD_CONFIGS must be valid JSON: {exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("TICKET_GUILD_CONFIGS must be a JSON object keyed by guild id.")

    configs: dict[str, dict[str, int]] = {}
    for guild_id, config in parsed.items():
        if not isinstance(config, dict) or "channel_id" not in config:
            raise RuntimeError(
                f"TICKET_GUILD_CONFIGS[{guild_id}] must include a channel_id."
            )

        try:
            normalized = {"channel_id": int(config["channel_id"])}
            for key, value in config.items():
                if (key == "category_id" or key.endswith("_category_id")) and value not in (None, ""):
                    normalized[key] = int(value)
            configs[str(int(guild_id))] = normalized
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"TICKET_GUILD_CONFIGS[{guild_id}] IDs must be integers."
            ) from exc

    return configs


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: "TicketingCog"):
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(TicketPanelButton(cog))


class TicketPanelButton(discord.ui.Button):
    def __init__(self, cog: "TicketingCog"):
        super().__init__(
            label="General Support",
            style=discord.ButtonStyle.primary,
            custom_id="ticketing:general_support",
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(GeneralSupportModal(self.cog))


class GeneralSupportModal(discord.ui.Modal, title="General Support"):
    issue = discord.ui.Label(
        text="Describe your issue:",
        component=discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=2000,
        ),
    )

    def __init__(self, cog: "TicketingCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_ticket(interaction, self.issue.component.value)


class TicketConfigView(discord.ui.View):
    def __init__(self, cog: "TicketingCog", guild_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.add_item(TicketTypeSelect(cog, guild_id))


class TicketTypeSelect(discord.ui.Select):
    def __init__(self, cog: "TicketingCog", guild_id: int):
        self.cog = cog
        self.guild_id = guild_id
        enabled = cog.get_enabled_types(guild_id)
        options = [
            discord.SelectOption(
                label=cfg["label"],
                value=key,
                default=key in enabled,
            )
            for key, cfg in TICKET_TYPES.items()
        ]
        options.append(
            discord.SelectOption(
                label="No buttons",
                value="__none__",
                default=not enabled,
            )
        )
        super().__init__(
            placeholder="Choose the buttons shown on the ticket panel",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = [] if "__none__" in self.values else self.values
        self.cog.set_enabled_types(self.guild_id, selected)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.upsert_panel(interaction.guild)
        await interaction.followup.send("Ticket panel updated.", ephemeral=True)


class TicketingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_configs = parse_ticket_guild_configs(
            os.getenv("TICKET_GUILD_CONFIGS", "")
        )
        self.state_path = os.getenv("TICKET_STATE_PATH", "ticket_state.json")
        self.state = self._load_state()
        self.panel_messages: dict[int, discord.Message] = {}

    def _load_state(self) -> dict[str, Any]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as state_file:
                state = json.load(state_file)
                if isinstance(state, dict):
                    state.setdefault("counters", {})
                    state.setdefault("enabled_types", {})
                    return state
        except (OSError, json.JSONDecodeError):
            pass
        return {"counters": {}, "enabled_types": {}}

    def _save_state(self) -> None:
        try:
            with open(self.state_path, "w", encoding="utf-8") as state_file:
                json.dump(self.state, state_file)
        except OSError:
            pass

    def get_enabled_types(self, guild_id: int) -> list[str]:
        enabled_types = self.state["enabled_types"].get(str(guild_id))
        if enabled_types is None:
            return list(TICKET_TYPES)
        return [key for key in enabled_types if key in TICKET_TYPES]

    def set_enabled_types(self, guild_id: int, enabled_types: list[str]) -> None:
        self.state["enabled_types"][str(guild_id)] = [
            key for key in enabled_types if key in TICKET_TYPES
        ]
        self._save_state()

    async def _configured_channel(self, guild: discord.Guild):
        config = self.guild_configs.get(str(guild.id))
        if config is None:
            logger.warning(
                "Ticket panel skipped for guild %s: add its channel_id to TICKET_GUILD_CONFIGS.",
                guild.id,
            )
            return None
        channel = guild.get_channel(config["channel_id"])
        if channel is None:
            try:
                channel = await guild.fetch_channel(config["channel_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "Could not fetch ticket panel channel %s for guild %s: %s",
                    config["channel_id"],
                    guild.id,
                    exc,
                )
                return None
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "Ticket panel channel %s for guild %s is not a text channel.",
                config["channel_id"],
                guild.id,
            )
            return None
        if channel.guild.id != guild.id:
            logger.error(
                "Ticket panel channel %s belongs to guild %s, not configured guild %s.",
                channel.id,
                channel.guild.id,
                guild.id,
            )
            return None
        return channel

    async def upsert_panel(self, guild: discord.Guild | None) -> None:
        if guild is None:
            return
        channel = await self._configured_channel(guild)
        if channel is None:
            return

        enabled = self.get_enabled_types(guild.id)
        view = TicketPanelView(self) if "general_support" in enabled else None
        embed = discord.Embed(
            title="Make a ticket:",
            description=(
                "Should you need assistance with anything, create a ticket below. "
                "Our staff will get to your ticket as soon as possible; Please be patient. "
            ),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text="zvtbot-ticket-panel-v1")

        message = self.panel_messages.get(guild.id)
        if message is not None:
            try:
                await message.edit(embed=embed, view=view)
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                self.panel_messages.pop(guild.id, None)

        try:
            async for candidate in channel.history(limit=100):
                if (
                    candidate.author == self.bot.user
                    and candidate.embeds
                    and candidate.embeds[0].footer.text == "zvtbot-ticket-panel-v1"
                ):
                    self.panel_messages[guild.id] = candidate
                    if view is not None:
                        self.bot.add_view(view, message_id=candidate.id)
                    await candidate.edit(embed=embed, view=view)
                    return
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "Could not check ticket panel history in channel %s: %s; trying to post the panel anyway.",
                channel.id,
                exc,
            )

        try:
            message = await channel.send(embed=embed, view=view)
            self.panel_messages[guild.id] = message
            if view is not None:
                self.bot.add_view(view, message_id=message.id)
            logger.info(
                "Posted ticket panel in #%s for guild %s.", channel.name, guild.id
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.error(
                "Could not post ticket panel in channel %s for guild %s: %s",
                channel.id,
                guild.id,
                exc,
            )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self.upsert_panel(guild)

    @app_commands.command(
        name="ticketconfig",
        description="Choose which buttons appear on this server's ticket panel",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def ticketconfig(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command is only available in a server.", ephemeral=True
            )
            return
        if str(interaction.guild.id) not in self.guild_configs:
            await interaction.response.send_message(
                "This server has no TICKET_GUILD_CONFIGS entry.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Choose the ticket buttons to display:",
            view=TicketConfigView(self, interaction.guild.id),
            ephemeral=True,
        )

    async def create_ticket(
        self, interaction: discord.Interaction, issue: str
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Tickets can only be created in a server.", ephemeral=True
            )
            return

        config = self.guild_configs.get(str(guild.id))
        if config is None:
            await interaction.response.send_message(
                "Ticketing is not configured for this server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        ticket_type = TICKET_TYPES["general_support"]
        category = None
        category_id = config.get(ticket_type["category_key"])
        if category_id is not None:
            resolved_category = guild.get_channel(category_id)
            if resolved_category is None:
                try:
                    resolved_category = await guild.fetch_channel(category_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    resolved_category = None
            if isinstance(resolved_category, discord.CategoryChannel):
                category = resolved_category
            else:
                await interaction.followup.send(
                    "The configured ticket category could not be found. Check TICKET_GUILD_CONFIGS.",
                    ephemeral=True,
                )
                return

        counters = self.state["counters"]
        last_ticket_number = int(counters.get(str(guild.id), 0))
        existing_names = {channel.name for channel in guild.channels}
        ticket_number = None
        for offset in range(1, 10000):
            candidate_number = (last_ticket_number + offset - 1) % 9999 + 1
            if f"{ticket_type['channel_prefix']}-{candidate_number:04d}" not in existing_names:
                ticket_number = candidate_number
                break
        if ticket_number is None:
            await interaction.followup.send(
                "No unique ticket number is available.", ephemeral=True
            )
            return
        counters[str(guild.id)] = ticket_number
        self._save_state()

        requester = interaction.user
        overwrites = dict(category.overwrites) if category is not None else {}
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        overwrites[requester] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )
        bot_member = guild.me
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
            )

        try:
            channel = await guild.create_text_channel(
                name=f"{ticket_type['channel_prefix']}-{ticket_number:04d}",
                category=category,
                overwrites=overwrites,
                reason="General support ticket created",
            )
            support_role = next(
                (
                    role
                    for role in getattr(guild, "roles", [])
                    if role.name == "Support Staff"
                ),
                None,
            )
            opening_embed = discord.Embed(
                description=(
                    f"{requester.mention} has opened a "
                    f"{ticket_type['category_name']} ticket."
                ),
                colour=discord.Colour.blurple(),
            )
            opening_embed.add_field(name="Question:", value=f"*{issue}*", inline=False)
            await channel.send(
                content=(
                    f"{requester.mention} - Thank you for opening a ticket. "
                    "Please be patient while staff gets to your issue.\n"
                    f"{support_role.mention if support_role else '@Support Staff'}"
                ),
                embed=opening_embed,
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I could not create the ticket channel. Check my channel permissions.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Your ticket is ready: {channel.mention}", ephemeral=True
        )