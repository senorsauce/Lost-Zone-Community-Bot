import json
import os

import discord
from discord.ext import commands
from typing import Any


class TicketingCog(commands.Cog):
    """Cog providing a persistent ticket panel and ticket channel creation."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ticket_config = self._load_ticket_config()
        # persistent state (counters etc.)
        self.state_path = os.getenv("TICKET_STATE_PATH", "ticket_state.json")
        self.state = self._load_state()
        # optional global archive category id
        self.global_archive_category = self._parse_int_env("TICKET_ARCHIVE_CATEGORY_ID")
        # optional panel channel id (post the persistent panel there on_ready)
        self.panel_channel_id = self._parse_int_env("TICKET_PANEL_CHANNEL_ID")

    def _parse_int_env(self, name: str) -> int | None:
        raw = os.getenv(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except Exception:
            return None

    def _resolve_emoji(self, emoji_raw: str) -> str | None:
        """Resolve a shortcode like :question: to a unicode emoji, or return the raw string if it's already an emoji."""
        if not emoji_raw:
            return None
        emoji_raw = str(emoji_raw).strip()
        # common shortcode mapping — extend as needed
        mapping = {
            ":question:": "❓",
            ":white_check_mark:": "✅",
            ":bug:": "🐛",
            ":x:": "❌",
            ":exclamation:": "❗",
            ":grey_question:": "❔",
        }
        if emoji_raw in mapping:
            return mapping[emoji_raw]
        # If user provided <:name:id> or a unicode emoji, return as-is
        return emoji_raw

    def _load_ticket_config(self) -> dict[str, Any]:
        raw = os.getenv("TICKET_CONFIG", "").strip()
        if not raw:
            # Example default config — users should override with JSON in env or a configuration file.
            return {
                "bug_report": {
                    "label": "Bug Report",
                    "description": "Report bugs to developers.",
                    "emoji": ":bug:",
                    "prefix": "BR",
                    "category_id": None,
                    "role_ids": []
                },
                "whitelist": {
                    "label": "Whitelist",
                    "description": "Request whitelist access.",
                    "emoji": ":white_check_mark:",
                    "prefix": "WL",
                    "category_id": None,
                    "role_ids": []
                }
            }

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}

        if not isinstance(parsed, dict):
            return {}

        # normalize IDs and emoji shortcodes for each ticket type
        for key, cfg in list(parsed.items()):
            if not isinstance(cfg, dict):
                continue
            # normalize category ids
            cat = cfg.get("category_id")
            if cat is None:
                cfg["category_id"] = None
            else:
                try:
                    cfg["category_id"] = int(cat)
                except Exception:
                    cfg["category_id"] = None

            # normalize archive category id per-type
            a_cat = cfg.get("archive_category_id")
            if a_cat is None:
                cfg["archive_category_id"] = None
            else:
                try:
                    cfg["archive_category_id"] = int(a_cat)
                except Exception:
                    cfg["archive_category_id"] = None

            # normalize role ids to list of ints
            roles = cfg.get("role_ids") or []
            normalized_roles: list[int] = []
            if isinstance(roles, list):
                for r in roles:
                    try:
                        normalized_roles.append(int(r))
                    except Exception:
                        continue
            elif isinstance(roles, (str, int)):
                try:
                    normalized_roles.append(int(roles))
                except Exception:
                    pass
            cfg["role_ids"] = normalized_roles

            # normalize emoji shortcodes — keep the raw but also store resolved emoji
            emoji_raw = cfg.get("emoji")
            cfg["emoji_raw"] = emoji_raw
            cfg["emoji_resolved"] = self._resolve_emoji(emoji_raw)

        return parsed

    def _load_state(self) -> dict[str, Any]:
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, dict):
                        return data
        except Exception:
            pass
        # default structure
        return {"counters": {}}

    def _save_state(self) -> None:
        try:
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
        except Exception:
            pass

    @commands.command(name="post_ticket_panel")
    @commands.has_permissions(manage_guild=True)
    async def post_ticket_panel(self, ctx: commands.Context) -> None:
        """Posts a persistent ticket panel with a select menu for ticket types."""
        options = []
        for key, cfg in self.ticket_config.items():
            label = cfg.get("label") or key
            desc = cfg.get("description", "")
            emoji = cfg.get("emoji_resolved") or cfg.get("emoji_raw") or cfg.get("emoji")
            options.append(discord.SelectOption(label=label, description=desc, emoji=emoji, value=key))

        if not options:
            await ctx.send("No ticket types are configured.")
            return

        class TicketSelect(discord.ui.Select):
            def __init__(self, parent: "TicketingCog"):
                super().__init__(placeholder="Select a topic", min_values=1, max_values=1, options=options)
                self.parent = parent

            async def callback(self, interaction: discord.Interaction) -> None:
                key = self.values[0]
                await self.parent._handle_ticket_request(interaction, key)

        class TicketView(discord.ui.View):
            def __init__(self, parent: "TicketingCog"):
                super().__init__(timeout=None)
                self.add_item(TicketSelect(parent))

        embed = discord.Embed(title="Ticket Center", description="If you require support, create a ticket by selecting a topic below.")
        await ctx.send(embed=embed, view=TicketView(self))

    async def _handle_ticket_request(self, interaction: discord.Interaction, key: str) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = interaction.user
        if guild is None:
            await interaction.followup.send("This command must be used in a server.")
            return

        cfg = self.ticket_config.get(key)
        if not cfg:
            await interaction.followup.send("Unknown ticket type selected.")
            return

        prefix = cfg.get("prefix", "T")
        # optional name format, supports {key} and {counter}
        name_format = cfg.get("name_format") or os.getenv("TICKET_NAME_FORMAT") or "{key}-ticket-{counter:02d}"
        category_id = cfg.get("category_id")
        role_ids = cfg.get("role_ids", []) or []

        # Resolve category
        category = None
        if category_id:
            try:
                category = guild.get_channel(int(category_id))
            except Exception:
                category = None

        # Increment and persist counter for this ticket type
        counters = self.state.setdefault("counters", {})
        cur = int(counters.get(key, 0)) + 1
        counters[key] = cur
        self._save_state()

        # Format channel name
        try:
            channel_name = name_format.format(key=key, prefix=prefix, counter=cur)
        except Exception:
            channel_name = f"{key}-ticket-{cur:02d}"

        # Prepare overwrites: hide from @everyone, show to requester and allowed roles
        overwrites: dict = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        for rid in role_ids:
            try:
                role = guild.get_role(int(rid))
            except Exception:
                role = None
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        # Bot perms
        bot_member = guild.me or guild.get_member(self.bot.user.id)
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)

        # Create channel
        try:
            channel = await guild.create_text_channel(name=channel_name, category=category, overwrites=overwrites, reason="Ticket created via ticket panel")
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to create channels in this server.")
            return
        except Exception as exc:
            await interaction.followup.send(f"Failed to create ticket channel: {exc}")
            return

        # Send initial message with a close button
        view = discord.ui.View()

        async def close_callback(interact: discord.Interaction) -> None:
            # Only allow the ticket creator, members with manage_guild, or configured roles to close
            requester = member
            actor = interact.user
            allowed = False
            if actor.id == requester.id:
                allowed = True
            if interact.user.guild_permissions.manage_guild:
                allowed = True
            for rid in role_ids:
                role = guild.get_role(int(rid)) if rid else None
                if role and role in getattr(actor, "roles", []):
                    allowed = True
                    break

            if not allowed:
                await interact.response.send_message("You don't have permission to close this ticket.", ephemeral=True)
                return

            # Try to archive the ticket: move to archive category and lock the requester
            archive_cat_id = cfg.get("archive_category_id") or self.global_archive_category
            archived = False
            if archive_cat_id:
                try:
                    archive_category = guild.get_channel(int(archive_cat_id))
                except Exception:
                    archive_category = None
                try:
                    if archive_category is not None:
                        await channel.edit(category=archive_category)
                        # remove view for requester
                        await channel.set_permissions(requester, overwrite=discord.PermissionOverwrite(view_channel=False))
                        archived = True
                except Exception:
                    archived = False

            if not archived:
                # fallback: try to delete
                try:
                    await channel.delete(reason="Ticket closed")
                    try:
                        await interact.response.send_message("Ticket closed and channel deleted.", ephemeral=True)
                    except Exception:
                        pass
                except Exception:
                    # last resort: lock channel for requester
                    try:
                        await channel.set_permissions(requester, overwrite=discord.PermissionOverwrite(view_channel=False))
                        await interact.response.send_message("Ticket locked.", ephemeral=True)
                    except Exception:
                        await interact.response.send_message("Failed to archive or delete ticket.", ephemeral=True)

        close_button = discord.ui.Button(label="Close Ticket", style=discord.ButtonStyle.danger)
        close_button.callback = close_callback
        view.add_item(close_button)

        try:
            await channel.send(f"Ticket created for {member.mention}. A staff member will be with you shortly.", view=view)
        except Exception:
            pass

        await interaction.followup.send(f"Created ticket: {channel.mention}", ephemeral=True)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Post the ticket panel into the configured channel (if provided)
        if not self.panel_channel_id:
            return

        try:
            chan_id = int(self.panel_channel_id)
        except Exception:
            return

        # Try to find the channel in any guild
        channel = None
        for g in self.bot.guilds:
            channel = g.get_channel(chan_id)
            if channel is not None:
                break

        if channel is None:
            return

        # Check recent messages for our panel (embed title match)
        try:
            async for msg in channel.history(limit=50):
                if msg.author == self.bot.user and msg.embeds:
                    for e in msg.embeds:
                        if e.title == "Ticket Center":
                            return
        except Exception:
            pass

        # Post a new panel
        options = []
        for key, cfg in self.ticket_config.items():
            label = cfg.get("label") or key
            desc = cfg.get("description", "")
            emoji = cfg.get("emoji_resolved") or cfg.get("emoji_raw") or cfg.get("emoji")
            options.append(discord.SelectOption(label=label, description=desc, emoji=emoji, value=key))

        if not options:
            return

        class TicketSelect(discord.ui.Select):
            def __init__(self, parent: "TicketingCog"):
                super().__init__(placeholder="Select a topic", min_values=1, max_values=1, options=options)
                self.parent = parent

            async def callback(self, interaction: discord.Interaction) -> None:
                key = self.values[0]
                await self.parent._handle_ticket_request(interaction, key)

        class TicketView(discord.ui.View):
            def __init__(self, parent: "TicketingCog"):
                super().__init__(timeout=None)
                self.add_item(TicketSelect(parent))

        embed = discord.Embed(title="Ticket Center", description="If you require support, create a ticket by selecting a topic below.")
        try:
            await channel.send(embed=embed, view=TicketView(self))
        except Exception:
            pass


def setup(bot: commands.Bot) -> None:
    bot.add_cog(TicketingCog(bot))
