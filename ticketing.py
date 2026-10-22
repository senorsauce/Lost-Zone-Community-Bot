import json
import io
import logging
import os
import re
import zipfile
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands


logger = logging.getLogger(__name__)

PANEL_FOOTER = "STALKER: Lost Zone Ticket Bot - Stalker Support"
LEGACY_PANEL_FOOTER = "zvtbot-ticket-panel-v1"
CLOSE_REQUEST_FOOTER = "ticket-close-request-v1"
DEFAULT_TRANSCRIPT_CHANNEL_ID = 1562225728651657337
DEFAULT_TRANSCRIPT_CATEGORY_ID = 1522884583471714324
ALLOWED_FILE_EXTENSIONS = {".jpg", ".png", ".json", ".zip", ".txt", ".mp3", ".mp4"}
TICKET_TYPES = {
    "general_support": {
        "label": "General Support",
        "precursor": (
            "Please ensure your ticket does not fit into one of the other categories "
            "before making a general submission. This organization serves to speed up "
            "response times and as a result, your ticket will take longer to be resolved."
        ),
        "emoji": "❓",
        "category_name": "General",
        "modal_title": "General Support",
        "channel_prefix": "gen",
        "questions": [
            {
                "label": "Describe your issue:",
                "answer_label": "Question:",
                "multiline": True,
                "max_length": 2000,
            }
        ],
    },
    "whitelist_submission": {
        "label": "Whitelist Submission",
        "precursor": (
            "Low effort whitelists will be met with immediate denial. Though lengthy "
            "answers are not required, they are encouraged, and may be used in decision "
            "making in the future. Read the rules prior to applying."
        ),
        "emoji": "✍️",
        "category_name": "Whitelist",
        "modal_title": "Whitelist Submission",
        "channel_prefix": "wls",
        "questions": [
            {"label": "What is your Character's Name?", "max_length": 100}
        ],
    },
    "warfare": {
        "label": "Warfare",
        "emoji": "⚔️",
        "category_name": "Warfare",
        "modal_title": "Warfare Report",
        "channel_prefix": "war",
        "questions": [
            {"label": "Group attacking:", "max_length": 200},
            {"label": "Node Being Attacked:", "max_length": 200},
            {
                "label": "Additional Information:",
                "multiline": True,
                "max_length": 2000,
            },
        ],
    },
    "rulebreaks": {
        "label": "Rulebreak Report",
        "precursor": (
            "By submitting a rulebreak ticket, you are accusing another player of "
            "breaking server rules, which may lead to that player incurring penalty. "
            "Though these matters are always thoroughly and seriously investigated, "
            "you must provide ample proof of all allegations."
        ),
        "emoji": "❌",
        "category_name": "Rulebreaks",
        "modal_title": "Rulebreak Report",
        "channel_prefix": "rbr",
        "questions": [
            {"label": "Player(s) involved:", "max_length": 500},
            {"label": "Player(s) being accused:", "max_length": 500},
            {"label": "Rule(s) being broken:", "max_length": 500},
            {
                "label": "Describe the incident:",
                "multiline": True,
                "max_length": 2000,
            },
        ],
        "acknowledgment": (
            "I have proof to back up my claims, and this submission is in good faith."
        ),
    },
    "admin_reports": {
        "label": "Admin Report",
        "precursor": (
            "By submitting an admin report, you are accusing a staff member of "
            "breaching the player bases' trust. Though these matters are always "
            "thoroughly and seriously investigated, you must provide ample proof of "
            "all allegations."
        ),
        "emoji": "🚨",
        "category_name": "Admin Reports",
        "modal_title": "Admin Report",
        "channel_prefix": "adm",
        "questions": [
            {"label": "Player(s) involved:", "max_length": 500},
            {"label": "Admin(s) accused:", "max_length": 500},
            {
                "label": "Describe the incident:",
                "multiline": True,
                "max_length": 2000,
            },
        ],
        "acknowledgment": (
            "I have proof to back up my claims, and this submission is in good faith."
        ),
    },
    "bug_report": {
        "label": "Bug Report",
        "precursor": (
            "Please be as thorough as possible when describing your bug to better "
            "help developers fix it ASAP."
        ),
        "emoji": "🪲",
        "category_name": "Bug Report",
        "modal_title": "Bug Report",
        "channel_prefix": "bug",
        "questions": [
            {"label": "How did this bug first occur?", "max_length": 1000},
            {
                "label": "Did you lose anything as a result of the bug?",
                "max_length": 1000,
            },
            {"label": "Is this bug repeatable?", "max_length": 1000},
            {"label": "Additional Comments? (Put N/A if no)", "max_length": 1000},
        ],
    },
    "compensation_request": {
        "label": "Compensation Request",
        "precursor": (
            "Our staff would like to rectify any situation which you believe to be "
            "unfair. As such, please provide thorough proof of the situation."
        ),
        "emoji": "🪙",
        "category_name": "Compensation Request",
        "modal_title": "Compensation Request",
        "channel_prefix": "cmp",
        "questions": [
            {
                "label": "What item(s) were lost, and require compensation?",
                "max_length": 1000,
            },
            {"label": "How did the loss occur?", "max_length": 1000},
        ],
        "acknowledgment": "I have proof of the situation.",
    },
}


def validate_upload_extension(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in ALLOWED_FILE_EXTENSIONS


def split_modal_label(label: str, max_length: int = 45) -> tuple[str, str | None]:
    if len(label) <= max_length:
        return label, None
    split_at = label.rfind(" ", 0, max_length + 1)
    if split_at <= 0:
        split_at = max_length
    return label[:split_at], label[split_at:].lstrip()


async def has_staff_role(interaction: discord.Interaction) -> bool:
    roles = getattr(interaction.user, "roles", ())
    return any(role.name.casefold() == "staff" for role in roles)


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
            configs[str(int(guild_id))] = normalized
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"TICKET_GUILD_CONFIGS[{guild_id}] IDs must be integers."
            ) from exc

    return configs


def parse_ticket_category_configs(raw: str) -> dict[str, dict[str, int]]:
    if not raw.strip():
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"TICKET_CATEGORY_CONFIGS must be valid JSON: {exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("TICKET_CATEGORY_CONFIGS must be a JSON object keyed by guild id.")

    configs: dict[str, dict[str, int]] = {}
    for guild_id, categories in parsed.items():
        if not isinstance(categories, dict):
            raise RuntimeError(f"TICKET_CATEGORY_CONFIGS[{guild_id}] must be an object.")
        try:
            configs[str(int(guild_id))] = {
                ticket_type: int(category_id)
                for ticket_type, category_id in categories.items()
                if category_id not in (None, "")
            }
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"TICKET_CATEGORY_CONFIGS[{guild_id}] IDs must be integers."
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
            emoji=config["emoji"],
            custom_id=f"ticketing:{ticket_type}",
        )
        self.cog = cog
        self.ticket_type_key = ticket_type

    async def callback(self, interaction: discord.Interaction) -> None:
        config = TICKET_TYPES[self.ticket_type_key]
        if config.get("precursor"):
            embed = discord.Embed(
                title=config["label"],
                description=config["precursor"],
                colour=discord.Colour(0x3C3CE8),
            )
            await interaction.response.send_message(
                embed=embed,
                view=TicketPrecursorView(self.cog, self.ticket_type_key),
                ephemeral=True,
            )
            return
        try:
            await interaction.response.send_modal(
                TicketSubmissionModal(self.cog, self.ticket_type_key)
            )
        except Exception:
            logger.exception(
                "Could not open %s ticket form.", self.ticket_type_key
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "The ticket form could not be opened. Please try again.",
                    ephemeral=True,
                )


class TicketPrecursorView(discord.ui.View):
    def __init__(self, cog: "TicketingCog", ticket_type: str):
        super().__init__(timeout=180)
        self.add_item(TicketPrecursorButton(cog, ticket_type))


class TicketPrecursorButton(discord.ui.Button):
    def __init__(self, cog: "TicketingCog", ticket_type: str):
        super().__init__(label="Continue", style=discord.ButtonStyle.secondary)
        self.cog = cog
        self.ticket_type = ticket_type

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.send_modal(
                TicketSubmissionModal(self.cog, self.ticket_type)
            )
        except (discord.HTTPException, ValueError):
            logger.exception(
                "Could not open %s ticket form after precursor.", self.ticket_type
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "The ticket form could not be opened. Please try again.",
                    ephemeral=True,
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
            label, description = split_modal_label(question["label"])
            self.add_item(
                discord.ui.Label(
                    text=label,
                    description=description,
                    component=field,
                )
            )
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

        await interaction.response.defer(ephemeral=True, thinking=True)
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
            await interaction.followup.send(
                "One or more uploaded files could not be read. Please try again.",
                ephemeral=True,
            )
            return

        acknowledgment = TICKET_TYPES[self.ticket_type].get("acknowledgment")
        if acknowledgment:
            await interaction.followup.send(
                "Please confirm the evidence acknowledgment to continue.",
                view=TicketAcknowledgmentView(
                    self.cog,
                    self.ticket_type,
                    answers,
                    acknowledgment,
                    uploads,
                ),
                ephemeral=True,
            )
            return

        await self.cog.finish_ticket_submission(
            interaction, answers, self.ticket_type, uploads
        )


class TicketAcknowledgmentModal(discord.ui.Modal, title="Final Acknowledgment"):
    def __init__(
        self,
        cog: "TicketingCog",
        ticket_type_key: str,
        answers: dict[str, str],
        acknowledgment: str,
        uploads: list[dict[str, Any]] | None = None,
    ):
        super().__init__()
        self.cog = cog
        self.ticket_type_key = ticket_type_key
        self.answers = answers
        self.uploads = uploads or []
        self.confirmation = discord.ui.CheckboxGroup(
            required=True,
            min_values=1,
            max_values=1,
            options=[
                discord.CheckboxGroupOption(
                    label=acknowledgment,
                    value="confirmed",
                )
            ],
        )
        self.add_item(
            discord.ui.Label(text="Required confirmation", component=self.confirmation)
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if "confirmed" not in self.confirmation.values:
            await interaction.response.send_message(
                "You must check the proof confirmation before submitting.",
                ephemeral=True,
            )
            return
        self.answers[TICKET_TYPES[self.ticket_type_key]["acknowledgment"]] = "Confirmed"
        await self.cog.finish_ticket_submission(
            interaction,
            self.answers,
            self.ticket_type_key,
            self.uploads,
        )


class TicketAcknowledgmentView(discord.ui.View):
    def __init__(
        self,
        cog: "TicketingCog",
        ticket_type_key: str,
        answers: dict[str, str],
        acknowledgment: str,
        uploads: list[dict[str, Any]],
    ):
        super().__init__(timeout=180)
        self.add_item(
            TicketAcknowledgmentButton(
                cog, ticket_type_key, answers, acknowledgment, uploads
            )
        )


class TicketAcknowledgmentButton(discord.ui.Button):
    def __init__(
        self,
        cog: "TicketingCog",
        ticket_type_key: str,
        answers: dict[str, str],
        acknowledgment: str,
        uploads: list[dict[str, Any]],
    ):
        super().__init__(label="Continue", style=discord.ButtonStyle.secondary)
        self.cog = cog
        self.ticket_type_key = ticket_type_key
        self.answers = answers
        self.acknowledgment = acknowledgment
        self.uploads = uploads

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.send_modal(
                TicketAcknowledgmentModal(
                    self.cog,
                    self.ticket_type_key,
                    self.answers,
                    self.acknowledgment,
                    self.uploads,
                )
            )
        except Exception:
            logger.exception(
                "Could not open acknowledgment modal for %s.",
                self.ticket_type_key,
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "The confirmation form could not be opened. Please try again.",
                    ephemeral=True,
                )


class GeneralSupportModal(TicketSubmissionModal):
    def __init__(self, cog: "TicketingCog"):
        super().__init__(cog, "general_support")


class BugReportModal(TicketSubmissionModal):
    def __init__(self, cog: "TicketingCog"):
        super().__init__(cog, "bug_report")


class CloseRequestModal(discord.ui.Modal, title="Request Ticket Closure"):
    reason = discord.ui.Label(
        text="Reason for closing",
        component=discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=1000,
        ),
    )

    def __init__(self, cog: "TicketingCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.post_close_request(interaction, self.reason.component.value)


class CloseRequestView(discord.ui.View):
    def __init__(self, cog: "TicketingCog", channel_id: int, owner_id: int):
        super().__init__(timeout=None)
        self.add_item(CloseRequestButton(cog, channel_id, owner_id))


class CloseRequestButton(discord.ui.Button):
    def __init__(self, cog: "TicketingCog", channel_id: int, owner_id: int):
        super().__init__(
            label="Accept",
            style=discord.ButtonStyle.success,
            custom_id=f"ticket-close-accept:{channel_id}:{owner_id}",
        )
        self.cog = cog
        self.channel_id = channel_id
        self.owner_id = owner_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.accept_close_request(
            interaction,
            self.channel_id,
            self.owner_id,
            interaction.message.id,
        )


class StaffCloseOverrideView(discord.ui.View):
    def __init__(
        self,
        cog: "TicketingCog",
        channel_id: int,
        owner_id: int,
        request_message_id: int,
    ):
        super().__init__(timeout=180)
        self.add_item(
            StaffCloseOverrideButton(
                cog, channel_id, owner_id, request_message_id
            )
        )


class StaffCloseOverrideButton(discord.ui.Button):
    def __init__(
        self,
        cog: "TicketingCog",
        channel_id: int,
        owner_id: int,
        request_message_id: int,
    ):
        super().__init__(
            label="Confirm staff override",
            style=discord.ButtonStyle.danger,
        )
        self.cog = cog
        self.channel_id = channel_id
        self.owner_id = owner_id
        self.request_message_id = request_message_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.accept_close_request(
            interaction,
            self.channel_id,
            self.owner_id,
            self.request_message_id,
            staff_override_confirmed=True,
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
        self.category_configs = parse_ticket_category_configs(
            os.getenv("TICKET_CATEGORY_CONFIGS", "")
        )
        self.bot_username = os.getenv("BOT_USERNAME", "stalker_lost_zone")
        self._global_username_checked = False
        self.state_path = os.getenv("TICKET_STATE_PATH", "ticket_state.json")
        self.bot_nickname = os.getenv("BOT_NICKNAME", "STALKER: Lost Zone")
        self.transcript_channel_id = int(
            os.getenv(
                "TICKET_TRANSCRIPT_CHANNEL_ID",
                str(DEFAULT_TRANSCRIPT_CHANNEL_ID),
            )
        )
        self.transcript_category_id = int(
            os.getenv(
                "TICKET_TRANSCRIPT_CATEGORY_ID",
                str(DEFAULT_TRANSCRIPT_CATEGORY_ID),
            )
        )
        self._close_request_views_registered = False
        self.state = self._load_state()
        if self.state.get("ticket_types_version", 1) < 7:
            for enabled_types in self.state["enabled_types"].values():
                if "general_support" in enabled_types:
                    enabled_types.extend(
                        ticket_type_key
                        for ticket_type_key in TICKET_TYPES
                        if ticket_type_key not in enabled_types
                    )
            self.state["ticket_types_version"] = 7
            self._save_state()
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

    def _is_ticket_channel(self, channel: Any) -> bool:
        if not isinstance(channel, discord.TextChannel):
            return False
        if "ticket_status=closed" in (channel.topic or ""):
            return False
        return any(
            channel.name.startswith(f"{ticket_type['channel_prefix']}-")
            for ticket_type in TICKET_TYPES.values()
        )

    async def _resolve_ticket_owner_id(self, channel: discord.TextChannel) -> int | None:
        topic = channel.topic or ""
        owner_match = re.search(r"(?:^|;)ticket_owner_id=(\d+)(?:;|$)", topic)
        if owner_match:
            return int(owner_match.group(1))

        try:
            async for message in channel.history(limit=25, oldest_first=True):
                if message.author == self.bot.user:
                    mentioned_users = getattr(message, "mentions", [])
                    if mentioned_users:
                        return mentioned_users[0].id
                    for content in [message.content] + [
                        embed.description or "" for embed in message.embeds
                    ]:
                        mention = re.search(r"<@!?(\d+)>", content or "")
                        if mention:
                            return int(mention.group(1))
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "Could not resolve ticket owner from channel %s history: %s",
                channel.id,
                exc,
            )
        return None

    async def _ticket_command_channel(
        self, interaction: discord.Interaction
    ) -> discord.TextChannel | None:
        channel = interaction.channel
        if not self._is_ticket_channel(channel):
            await self._send_ephemeral(
                interaction, "Use this command inside an open ticket channel."
            )
            return None
        return channel

    async def _get_transcript_channel(
        self, guild: discord.Guild
    ) -> discord.TextChannel | None:
        channel = guild.get_channel(self.transcript_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(self.transcript_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.error(
                    "Could not fetch transcript channel %s in guild %s: %s",
                    self.transcript_channel_id,
                    guild.id,
                    exc,
                )
                return None
        if not isinstance(channel, discord.TextChannel):
            logger.error(
                "Transcript channel %s is not a text channel.",
                self.transcript_channel_id,
            )
            return None
        if channel.guild.id != guild.id:
            logger.error(
                "Transcript channel %s is not in ticket guild %s.",
                channel.id,
                guild.id,
            )
            return None
        if channel.category_id != self.transcript_category_id:
            logger.error(
                "Transcript channel %s is not in configured category %s.",
                channel.id,
                self.transcript_category_id,
            )
            return None
        return channel

    async def _archive_ticket_transcript(
        self, ticket_channel: discord.TextChannel
    ) -> discord.Message:
        transcript_channel = await self._get_transcript_channel(ticket_channel.guild)
        if transcript_channel is None:
            raise RuntimeError(
                "Transcript channel is missing, inaccessible, or in the wrong category."
            )

        transcript_lines = [
            f"Ticket transcript: #{ticket_channel.name} ({ticket_channel.id})",
            f"Guild: {ticket_channel.guild.name} ({ticket_channel.guild.id})",
            "",
        ]
        attachments: list[tuple[str, bytes]] = []
        message_count = 0
        async for message in ticket_channel.history(limit=None, oldest_first=True):
            message_count += 1
            created_at = getattr(message, "created_at", None)
            timestamp = created_at.isoformat() if created_at is not None else "unknown time"
            author = getattr(message.author, "display_name", message.author.name)
            transcript_lines.append(
                f"[{timestamp}] {author} ({message.author.id})"
            )
            if message.content:
                transcript_lines.append(message.content)

            for embed in message.embeds:
                if embed.title:
                    transcript_lines.append(f"Embed title: {embed.title}")
                if embed.description:
                    transcript_lines.append(embed.description)
                for field in embed.fields:
                    transcript_lines.append(f"{field.name}: {field.value}")

            for attachment in message.attachments:
                safe_name = os.path.basename(attachment.filename.replace("\\", "/"))
                if not safe_name:
                    safe_name = f"attachment-{attachment.id}"
                archive_path = f"attachments/{message.id}/{attachment.id}-{safe_name}"
                try:
                    attachment_data = await attachment.read(use_cached=True)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    raise RuntimeError(
                        f"Could not download ticket attachment {attachment.filename}."
                    ) from exc
                attachments.append((archive_path, attachment_data))
                transcript_lines.append(
                    f"Attachment archived as {archive_path} ({attachment.size} bytes)."
                )
            transcript_lines.append("")

        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(
            archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("transcript.txt", "\n".join(transcript_lines))
            for archive_path, attachment_data in attachments:
                archive.writestr(archive_path, attachment_data)
        archive_bytes = archive_buffer.getvalue()
        if len(archive_bytes) > ticket_channel.guild.filesize_limit:
            raise RuntimeError(
                "Transcript archive exceeds this server's upload size limit."
            )

        embed = discord.Embed(
            title=f"Closed ticket transcript: #{ticket_channel.name}",
            description=(
                f"Ticket channel ID: {ticket_channel.id}\n"
                f"Messages: {message_count}\n"
                f"Files: {len(attachments)}"
            ),
            colour=discord.Colour(0x3C3CE8),
        )
        embed.set_footer(text=PANEL_FOOTER)
        return await transcript_channel.send(
            embed=embed,
            file=discord.File(
                io.BytesIO(archive_bytes),
                filename=f"{ticket_channel.name}-{ticket_channel.id}-transcript.zip",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _restore_close_request_views(self) -> None:
        if self._close_request_views_registered:
            return
        self._close_request_views_registered = True
        for guild in self.bot.guilds:
            for channel in getattr(guild, "text_channels", []):
                if not self._is_ticket_channel(channel):
                    continue
                try:
                    async for message in channel.history(limit=50):
                        if (
                            message.author == self.bot.user
                            and message.embeds
                            and CLOSE_REQUEST_FOOTER
                            in (message.embeds[0].footer.text or "")
                        ):
                            owner_id = await self._resolve_ticket_owner_id(channel)
                            if owner_id is not None:
                                view = CloseRequestView(self, channel.id, owner_id)
                                self.bot.add_view(view, message_id=message.id)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning(
                        "Could not restore close-request buttons in channel %s: %s",
                        channel.id,
                        exc,
                    )

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
            title="Make a ticket:",
            description=(
                "Should you need assistance with anything, create a ticket below. "
                "Our staff will get to your ticket as soon as possible; Please be patient. "
            ),
            colour=discord.Colour(0x3C3CE8),
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
        if not self._global_username_checked:
            user = self.bot.user
            if user is None or user.name == self.bot_username:
                self._global_username_checked = True
            else:
                try:
                    await user.edit(username=self.bot_username)
                    self._global_username_checked = True
                    logger.info("Updated global bot username to %s.", self.bot_username)
                except (discord.Forbidden, discord.HTTPException, ValueError) as exc:
                    logger.warning(
                        "Could not update global bot username; change it in the Discord Developer Portal if needed: %s",
                        exc,
                    )
        for guild in self.bot.guilds:
            member = guild.me
            if member is not None and member.nick != self.bot_nickname:
                try:
                    await member.edit(
                        nick=self.bot_nickname,
                        reason="Set configured community bot nickname",
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning(
                        "Could not set bot nickname in guild %s: %s", guild.id, exc
                    )
            await self.upsert_panel(guild)
        await self._restore_close_request_views()

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

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await self._send_ephemeral(
                interaction, "These ticket commands are only available to Staff."
            )
            return
        logger.error(
            "Ticket app command failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        await self._send_ephemeral(
            interaction, "The ticket command failed. Please try again."
        )

    @app_commands.command(
        name="useradd", description="Add a server member to this ticket"
    )
    @app_commands.guild_only()
    @app_commands.check(has_staff_role)
    @app_commands.describe(user="Server member to add to this ticket")
    async def useradd(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        channel = await self._ticket_command_channel(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(user)
        overwrite.view_channel = True
        overwrite.send_messages = True
        overwrite.read_message_history = True
        try:
            await channel.set_permissions(
                user,
                overwrite=overwrite,
                reason=f"Added to ticket by {interaction.user}",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Could not add member %s to ticket channel %s.", user.id, channel.id
            )
            await self._send_ephemeral(
                interaction, "I could not add that member to this ticket."
            )
            return
        await self._send_ephemeral(
            interaction, f"Added {user.mention} to this ticket."
        )

    @app_commands.command(
        name="userremove", description="Remove a server member from this ticket"
    )
    @app_commands.guild_only()
    @app_commands.check(has_staff_role)
    @app_commands.describe(user="Server member to remove from this ticket")
    async def userremove(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        channel = await self._ticket_command_channel(interaction)
        if channel is None:
            return
        try:
            await channel.set_permissions(
                user,
                overwrite=discord.PermissionOverwrite(
                    view_channel=False,
                    send_messages=False,
                    read_message_history=False,
                ),
                reason=f"Removed from ticket by {interaction.user}",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Could not remove member %s from ticket channel %s.",
                user.id,
                channel.id,
            )
            await self._send_ephemeral(
                interaction, "I could not remove that member from this ticket."
            )
            return
        await self._send_ephemeral(
            interaction, f"Removed {user.mention} from this ticket."
        )

    @app_commands.command(
        name="closerequest", description="Request that the ticket owner close this ticket"
    )
    @app_commands.guild_only()
    @app_commands.check(has_staff_role)
    async def closerequest(self, interaction: discord.Interaction) -> None:
        channel = await self._ticket_command_channel(interaction)
        if channel is None:
            return
        try:
            await interaction.response.send_modal(CloseRequestModal(self))
        except (discord.HTTPException, ValueError):
            logger.exception(
                "Could not open ticket close request modal in channel %s.", channel.id
            )
            await self._send_ephemeral(
                interaction, "The close request form could not be opened. Please try again."
            )

    async def post_close_request(
        self, interaction: discord.Interaction, reason: str
    ) -> None:
        channel = interaction.channel
        if not self._is_ticket_channel(channel):
            await self._send_ephemeral(
                interaction, "This channel is not an open ticket."
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        owner_id = await self._resolve_ticket_owner_id(channel)
        if owner_id is None:
            await interaction.followup.send(
                "I could not determine who opened this ticket, so I cannot create a close request.",
                ephemeral=True,
            )
            return

        staff_role = discord.utils.find(
            lambda role: role.name.casefold() == "staff", channel.guild.roles
        )
        if staff_role is None:
            await interaction.followup.send(
                "This server does not have a Staff role to mention.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Ticket closure request",
            description=(
                f"<@{owner_id}>, {staff_role.mention} has requested to close this "
                f"ticket with reason : {reason}. Click below to accept."
            ),
            colour=discord.Colour(0x3C3CE8),
        )
        embed.set_footer(text=f"{PANEL_FOOTER} | {CLOSE_REQUEST_FOOTER}")
        view = CloseRequestView(self, channel.id, owner_id)
        try:
            message = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(users=True, roles=True),
            )
            self.bot.add_view(view, message_id=message.id)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Could not post close request to ticket channel %s.", channel.id
            )
            await interaction.followup.send(
                "I could not post the close request in this ticket.", ephemeral=True
            )
            return

        await interaction.followup.send(
            "Close request posted in this ticket.", ephemeral=True
        )

    async def accept_close_request(
        self,
        interaction: discord.Interaction,
        channel_id: int,
        owner_id: int,
        request_message_id: int | None = None,
        staff_override_confirmed: bool = False,
    ) -> None:
        channel = interaction.channel
        if (
            channel is None
            or channel.id != channel_id
            or not self._is_ticket_channel(channel)
        ):
            await interaction.response.send_message(
                "This close request is not for an open ticket in the current channel.",
                ephemeral=True,
            )
            return

        is_owner = interaction.user.id == owner_id
        is_staff = await has_staff_role(interaction)
        if not is_owner:
            if not is_staff:
                await interaction.response.send_message(
                    "Only the ticket opener or Staff can accept this close request.",
                    ephemeral=True,
                )
                return
            if not staff_override_confirmed:
                await interaction.response.send_message(
                    "You did not open this ticket. Confirm that you want to close it "
                    "as a Staff override.",
                    view=StaffCloseOverrideView(
                        self,
                        channel_id,
                        owner_id,
                        request_message_id or interaction.message.id,
                    ),
                    ephemeral=True,
                )
                return

        requester = interaction.user
        if not is_owner:
            requester = channel.guild.get_member(owner_id)
            if requester is None:
                try:
                    requester = await channel.guild.fetch_member(owner_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    requester = None
            if requester is None:
                await interaction.response.send_message(
                    "I could not find the ticket opener in this server. The ticket was not closed.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if request_message_id is not None:
                request_message = channel.get_partial_message(request_message_id)
                request_message = await request_message.fetch()
            else:
                request_message = interaction.message
            if not request_message.embeds:
                raise RuntimeError("The close request message has no confirmation embed.")

            await self._archive_ticket_transcript(channel)
            previous_overwrite = channel.overwrites_for(requester)
            closed_overwrite = previous_overwrite
            closed_overwrite.view_channel = True
            closed_overwrite.read_message_history = True
            closed_overwrite.send_messages = False
            try:
                await channel.set_permissions(
                    requester,
                    overwrite=closed_overwrite,
                    reason=(
                        "Ticket close request accepted by opener"
                        if is_owner
                        else f"Ticket closed by Staff override from {interaction.user}"
                    ),
                )
                topic = channel.topic or ""
                if "ticket_status=closed" not in topic:
                    topic = (
                        f"{topic};ticket_status=closed"
                        if topic
                        else "ticket_status=closed"
                    )
                await channel.edit(topic=topic)
            except Exception:
                try:
                    await channel.set_permissions(
                        requester,
                        overwrite=previous_overwrite,
                        reason="Roll back incomplete ticket closure",
                    )
                except Exception:
                    logger.exception(
                        "Could not roll back permissions after closure failure in %s.",
                        channel.id,
                    )
                raise

            accepted_embed = request_message.embeds[0].copy()
            accepted_embed.title = "Ticket closed"
            accepted_embed.set_footer(text=f"{PANEL_FOOTER} | closed")
            try:
                await request_message.edit(embed=accepted_embed, view=None)
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Ticket %s was closed, but close-request message %s was not updated.",
                    channel.id,
                    request_message.id,
                )
        except Exception as exc:
            logger.exception("Could not close ticket channel %s.", channel.id)
            await interaction.followup.send(
                "I could not save the transcript or close this ticket. It remains open. "
                f"Details: {exc}",
                ephemeral=True,
            )
            return

        await interaction.followup.send("Ticket closed. You can still read its history.", ephemeral=True)

    async def finish_ticket_submission(
        self,
        interaction: discord.Interaction,
        answers: dict[str, str],
        ticket_type_key: str,
        uploads: list[dict[str, Any]],
    ) -> None:
        await self.create_ticket(interaction, answers, ticket_type_key, uploads)

    async def create_ticket(
        self,
        interaction: discord.Interaction,
        answers: dict[str, str],
        ticket_type: str,
        uploads: list[dict[str, Any]],
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self._send_ephemeral(interaction, "Tickets can only be created in a server.")
            return

        config = self.guild_configs.get(str(guild.id))
        if config is None:
            await self._send_ephemeral(interaction, "Ticketing is not configured for this server.")
            return

        if ticket_type not in TICKET_TYPES:
            await self._send_ephemeral(interaction, "Unknown ticket type.")
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        ticket_type_config = TICKET_TYPES[ticket_type]
        category = None
        category_id = self.category_configs.get(str(guild.id), {}).get(ticket_type)
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
                    f"The configured {ticket_type_config['category_name']} ticket category could not be found. "
                    "Check TICKET_CATEGORY_CONFIGS.",
                    ephemeral=True,
                )
                return
        else:
            category = next(
                (
                    candidate
                    for candidate in getattr(guild, "categories", [])
                    if candidate.name.casefold()
                    == ticket_type_config["category_name"].casefold()
                ),
                None,
            )
            if category is None:
                await interaction.followup.send(
                    f"Create a '{ticket_type_config['category_name']}' category or configure "
                    f"'{ticket_type}' in TICKET_CATEGORY_CONFIGS.",
                    ephemeral=True,
                )
                return

        counters = self.state["counters"]
        last_ticket_number = int(counters.get(str(guild.id), 0))
        existing_names = {channel.name for channel in guild.channels}
        ticket_number = None
        for offset in range(1, 10000):
            candidate_number = (last_ticket_number + offset - 1) % 9999 + 1
            candidate_name = f"{ticket_type_config['channel_prefix']}-{candidate_number:04d}"
            if candidate_name not in existing_names:
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
                name=f"{ticket_type_config['channel_prefix']}-{ticket_number:04d}",
                category=category,
                topic=f"ticket_type={ticket_type};ticket_owner_id={requester.id}",
                overwrites=overwrites,
                reason=f"{ticket_type_config['label']} ticket created",
            )
            opening_embed = discord.Embed(
                title=ticket_type_config["label"],
                        description=(
                            f"{requester.mention} has opened a "
                            f"{ticket_type_config['label']} ticket.\n"
                        ),
                colour=discord.Colour(0x3C3CE8),
            )
            display_name = getattr(
                requester,
                "display_name",
                getattr(requester, "name", str(requester)),
            )
            avatar = getattr(requester, "display_avatar", None)
            opening_embed.set_author(
                name=display_name,
                icon_url=getattr(avatar, "url", None),
            )
            opening_embed.set_footer(text=PANEL_FOOTER)
            for question in ticket_type_config["questions"]:
                answer = answers.get(question["label"], "")
                opening_embed.add_field(
                    name=question["label"],
                    value=answer or "N/A",
                    inline=False,
                )
            if acknowledgment := ticket_type_config.get("acknowledgment"):
                opening_embed.add_field(
                    name=acknowledgment,
                    value=answers.get(acknowledgment, "") or "N/A",
                    inline=False,
                )
            if ticket_type == "whitelist_submission":
                opening_content = (
                    f"Welcome to STALKER: Lost Zone, {requester.mention}! "
                    "Your whitelist has been received, and the answers have been "
                    "posted below for Staff review. Any corrections that need to "
                    "be made by you will be requested here.\n\n"
                    "Please be patient, thank you!"
                )
            else:
                opening_content = (
                    f"Hello {requester.mention} -- You have opened a "
                    f"{ticket_type_config['label']} ticket. Please be patient "
                    "while staff addresses your issue."
                )
            await channel.send(content=opening_content, embed=opening_embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not create or post the opening ticket message.")
            await interaction.followup.send(
                "I could not create the ticket channel. Check my channel permissions.",
                ephemeral=True,
            )
            return

        failed_uploads = []
        for upload in uploads:
            try:
                await channel.send(
                    file=discord.File(
                        io.BytesIO(upload["data"]), filename=upload["filename"]
                    )
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.exception(
                    "Could not attach uploaded file %s to ticket channel %s.",
                    upload["filename"],
                    channel.id,
                )
                failed_uploads.append(upload["filename"])

        if failed_uploads:
            await interaction.followup.send(
                "Ticket created, but these files could not be attached: "
                + ", ".join(failed_uploads)
                + ". They may exceed the server's upload limit.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Your ticket is ready: {channel.mention}", ephemeral=True
        )

    @staticmethod
    async def _send_ephemeral(interaction: discord.Interaction, content: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)