import json
import io
import logging
import os
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands


logger = logging.getLogger(__name__)

PANEL_FOOTER = "STALKER: Lost Zone Ticket Bot - Stalker Support"
LEGACY_PANEL_FOOTER = "zvtbot-ticket-panel-v1"
ALLOWED_FILE_EXTENSIONS = {".jpg", ".png", ".json", ".zip", ".txt", ".mp3", ".mp4"}

TICKET_TYPES = {
    "general_support": {
        "label": "General Support",
        "modal_title": "General Support",
        "channel_prefix": "support",
        "questions": [
            {"label": "Describe your issue:", "multiline": True, "max_length": 1000}
        ],
    },
    "bug_report": {
        "label": "Bug Report",
        "modal_title": "Bug Report",
        "channel_prefix": "bug",
        "questions": [
            {"label": "How did this bug first occur?", "max_length": 1000},
            {"label": "Did you lose anything as a result of the bug?", "max_length": 1000},
            {"label": "Is this bug repeatable?", "max_length": 1000},
            {"label": "Additional Comments? (Put N/A if no)", "max_length": 1000},
        ],
    },
}


def validate_upload_extension(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in ALLOWED_FILE_EXTENSIONS


def prepare_named_uploads(
    names_text: str, uploads: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]] | None, str | None]:
    names = [name.strip() for name in names_text.splitlines()]
    if len(names) != len(uploads) or any(not name for name in names):
        return None, f"Enter exactly {len(uploads)} non-empty filename(s), one per line."

    prepared: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for name, upload in zip(names, uploads):
        original_extension = os.path.splitext(upload["original_name"])[1].lower()
        if not validate_upload_extension(upload["original_name"]):
            return None, f"Unsupported file format: {upload['original_name']}"
        if "/" in name or "\\" in name or "\0" in name or name in {".", ".."}:
            return None, "File names cannot contain path separators."
        if not os.path.splitext(name)[1]:
            name += original_extension
        extension = os.path.splitext(name)[1].lower()
        if extension not in ALLOWED_FILE_EXTENSIONS or extension != original_extension:
            return None, f"Use the original accepted file extension for {upload['original_name']}."
        if len(name) > 100:
            return None, "File names must be 100 characters or fewer."
        if name.casefold() in seen_names:
            return None, "Each uploaded file needs a unique name."
        seen_names.add(name.casefold())
        prepared.append({"filename": name, "data": upload["data"]})

    return prepared, None


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
            if config.get("category_id") not in (None, ""):
                normalized["category_id"] = int(config["category_id"])
            configs[str(int(guild_id))] = normalized
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"TICKET_GUILD_CONFIGS[{guild_id}] IDs must be integers."
            ) from exc

    return configs


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: "TicketingCog", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        for ticket_type in cog.get_enabled_types(guild_id):
            self.add_item(TicketPanelButton(cog, ticket_type))


class TicketPanelButton(discord.ui.Button):
    def __init__(self, cog: "TicketingCog", ticket_type: str):
        config = TICKET_TYPES[ticket_type]
        super().__init__(
            label=config["label"],
            style=discord.ButtonStyle.secondary,
            custom_id=f"ticketing:{ticket_type}",
        )
        self.cog = cog
        self.ticket_type = ticket_type

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            TicketSubmissionModal(self.cog, self.ticket_type)
        )


class TicketSubmissionModal(discord.ui.Modal):
    def __init__(self, cog: "TicketingCog", ticket_type: str):
        config = TICKET_TYPES[ticket_type]
        super().__init__(title=config["modal_title"])
        self.cog = cog
        self.ticket_type = ticket_type
        self.question_fields: list[tuple[str, discord.ui.TextInput]] = []
        for question in config["questions"]:
            field = discord.ui.TextInput(
                style=(
                    discord.TextStyle.paragraph
                    if question.get("multiline")
                    else discord.TextStyle.short
                ),
                max_length=question["max_length"],
            )
            self.add_item(discord.ui.Label(text=question["label"], component=field))
            self.question_fields.append((question["label"], field))

        self.upload_field = discord.ui.Label(
            text="Upload files",
            description="Optional. Accepted: .jpg, .png, .json, .zip, .txt, .mp3, .mp4",
            component=discord.ui.FileUpload(
                required=False,
                min_values=0,
                max_values=10,
            ),
        )
        self.add_item(self.upload_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        answers = {label: field.value for label, field in self.question_fields}
        attachments = self.upload_field.component.values
        invalid_files = [
            attachment.filename
            for attachment in attachments
            if not validate_upload_extension(attachment.filename)
        ]
        if invalid_files:
            await interaction.response.send_message(
                "Unsupported file format(s): "
                + ", ".join(invalid_files)
                + ". Accepted formats: .jpg, .png, .json, .zip, .txt, .mp3, .mp4.",
                ephemeral=True,
            )
            return

        uploads = []
        try:
            for attachment in attachments:
                uploads.append(
                    {
                        "original_name": attachment.filename,
                        "data": await attachment.read(),
                    }
                )
        except (discord.HTTPException, OSError):
            await interaction.response.send_message(
                "One or more uploaded files could not be read. Please try again.",
                ephemeral=True,
            )
            return

        pending = {
            "ticket_type": self.ticket_type,
            "answers": answers,
            "uploads": uploads,
        }
        if uploads:
            await interaction.response.send_message(
                f"Upload received. Name each of the {len(uploads)} file(s) in upload order.",
                view=FileNamingView(self.cog, pending),
                ephemeral=True,
            )
            return

        await self.cog.create_ticket(interaction, answers, self.ticket_type, [])


class GeneralSupportModal(TicketSubmissionModal):
    def __init__(self, cog: "TicketingCog"):
        super().__init__(cog, "general_support")


class BugReportModal(TicketSubmissionModal):
    def __init__(self, cog: "TicketingCog"):
        super().__init__(cog, "bug_report")


class FileNamingView(discord.ui.View):
    def __init__(self, cog: "TicketingCog", pending: dict[str, Any]):
        super().__init__(timeout=180)
        self.add_item(FileNamingButton(cog, pending))


class FileNamingButton(discord.ui.Button):
    def __init__(self, cog: "TicketingCog", pending: dict[str, Any]):
        super().__init__(label="Name uploaded files", style=discord.ButtonStyle.secondary)
        self.cog = cog
        self.pending = pending

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(FileNamingModal(self.cog, self.pending))


class FileNamingModal(discord.ui.Modal, title="Name uploaded files"):
    def __init__(self, cog: "TicketingCog", pending: dict[str, Any]):
        super().__init__()
        self.cog = cog
        self.pending = pending
        self.names = discord.ui.Label(
            text="File names",
            description="One name per line, in upload order. Keep each file's original extension.",
            component=discord.ui.TextInput(
                style=discord.TextStyle.paragraph,
                placeholder="Example: evidence.jpg",
                max_length=1000,
            ),
        )
        self.add_item(self.names)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        uploads, error = prepare_named_uploads(
            self.names.component.value,
            self.pending["uploads"],
        )
        if error:
            await interaction.response.send_message(
                f"{error} Please enter one filename per line.",
                view=FileNamingView(self.cog, self.pending),
                ephemeral=True,
            )
            return
        await self.cog.create_ticket(
            interaction,
            self.pending["answers"],
            self.pending["ticket_type"],
            uploads or [],
        )


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
        view = TicketPanelView(self, guild.id) if enabled else None
        embed = discord.Embed(
            title="Ticket types:",
            description="Select a button below to contact the support team.",
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text=PANEL_FOOTER)

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
                    and candidate.embeds[0].footer.text
                    in {PANEL_FOOTER, LEGACY_PANEL_FOOTER}
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
        self,
        interaction: discord.Interaction,
        answers: dict[str, str],
        ticket_type: str,
        uploads: list[dict[str, Any]],
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

        if ticket_type not in TICKET_TYPES:
            await interaction.response.send_message(
                "Unknown ticket type.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        category = None
        category_id = config.get("category_id")
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

            counter_key = f"{guild.id}:{ticket_type}"
            counters = self.state["counters"]
            ticket_number = int(counters.get(counter_key, 0)) + 1
            counters[counter_key] = ticket_number
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
            ticket_config = TICKET_TYPES[ticket_type]
            channel = await guild.create_text_channel(
                name=f"{ticket_config['channel_prefix']}-{ticket_number:04d}",
                category=category,
                overwrites=overwrites,
                reason=f"{ticket_config['label']} ticket created",
            )
            embed = discord.Embed(
                title=ticket_config["label"],
                description=f"Ticket opened by {requester.mention}.",
                colour=discord.Colour.blurple(),
            )
            for question, answer in answers.items():
                embed.add_field(
                    name=question,
                    value=answer or "N/A",
                    inline=False,
                )
            files = [
                discord.File(io.BytesIO(upload["data"]), filename=upload["filename"])
                for upload in uploads
            ]
            await channel.send(embed=embed, files=files)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I could not create the ticket channel. Check my channel permissions.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Your ticket is ready: {channel.mention}", ephemeral=True
        )