import os
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ticketing import (
    GeneralSupportModal,
    TicketPanelView,
    TicketingCog,
    WhitelistSubmissionModal,
    parse_ticket_category_configs,
    parse_ticket_guild_configs,
)


class TicketGuildConfigTests(unittest.TestCase):
    def test_parses_per_guild_panel_channels(self):
        configs = parse_ticket_guild_configs(
            '{"123": {"channel_id": "456", "category_id": "789"}, '
            '"234": {"channel_id": 567}}'
        )

        self.assertEqual(
            configs,
            {"123": {"channel_id": 456}, "234": {"channel_id": 567}},
        )

    def test_parses_ticket_categories_independently_per_guild(self):
        configs = parse_ticket_category_configs(
            '{"123": {"general_support": "789", "whitelist_submission": 987}, '
            '"234": {"general_support": 654}}'
        )

        self.assertEqual(
            configs,
            {
                "123": {"general_support": 789, "whitelist_submission": 987},
                "234": {"general_support": 654},
            },
        )

    def test_rejects_missing_panel_channel(self):
        with self.assertRaisesRegex(RuntimeError, "must include a channel_id"):
            parse_ticket_guild_configs('{"123": {"category_id": 789}}')


class TicketPanelStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_migration_adds_whitelist_to_existing_panel_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "ticket_state.json")
            with open(state_path, "w", encoding="utf-8") as state_file:
                json.dump(
                    {
                        "ticket_types_version": 2,
                        "counters": {},
                        "enabled_types": {"123": ["general_support"]},
                    },
                    state_file,
                )

            with patch.dict(
                os.environ,
                {"TICKET_GUILD_CONFIGS": "", "TICKET_STATE_PATH": state_path},
            ):
                cog = TicketingCog(SimpleNamespace())

        self.assertEqual(
            cog.get_enabled_types(123),
            ["general_support", "whitelist_submission"],
        )

    async def test_missing_guild_config_is_logged(self):
        with patch.dict(
            os.environ,
            {"TICKET_GUILD_CONFIGS": "", "TICKET_STATE_PATH": os.devnull},
        ):
            cog = TicketingCog(SimpleNamespace())

        with self.assertLogs("ticketing", level="WARNING") as captured:
            await cog.upsert_panel(SimpleNamespace(id=123))

        self.assertIn("channel_id", captured.output[0])

    async def test_panel_has_requested_heading_and_persistent_button(self):
        bot = SimpleNamespace(user=object(), add_view=Mock())
        with patch.dict(os.environ, {"TICKET_STATE_PATH": os.devnull}):
            cog = TicketingCog(bot)

        posted = {}

        class Channel:
            name = "tickets"

            async def history(self, limit):
                if False:
                    yield None

            async def send(self, *, embed, view):
                posted["embed"] = embed
                posted["view"] = view
                return SimpleNamespace(id=456)

        guild = SimpleNamespace(id=123)
        with patch.object(cog, "_configured_channel", new=AsyncMock(return_value=Channel())):
            await cog.upsert_panel(guild)

        self.assertEqual(posted["embed"].title, "Make a ticket:")
        self.assertEqual(
            posted["embed"].description,
            "Should you need assistance with anything, create a ticket below. "
            "Our staff will get to your ticket as soon as possible; Please be patient. ",
        )
        self.assertIsNone(posted["view"].timeout)
        self.assertEqual(
            [button.label for button in posted["view"].children],
            ["General Support", "Whitelist Submission"],
        )
        self.assertEqual(posted["view"].children[0].emoji.name, "❓")
        self.assertEqual(posted["view"].children[1].emoji.name, "✍️")
        self.assertEqual(posted["embed"].colour.value, 0x3C3CE8)
        self.assertEqual(GeneralSupportModal(cog).children[0].text, "Describe your issue:")
        self.assertEqual(
            WhitelistSubmissionModal(cog).children[0].text,
            "What is your Character's Name?",
        )


class TicketCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_general_ticket_uses_category_unique_name_and_opening_message(self):
        bot = SimpleNamespace(user=object())
        config = '{"123": {"channel_id": 456}}'
        categories = '{"123": {"general_support": 789}}'
        with patch.dict(
            os.environ,
            {
                "TICKET_GUILD_CONFIGS": config,
                "TICKET_CATEGORY_CONFIGS": categories,
                "TICKET_STATE_PATH": os.devnull,
            },
        ):
            cog = TicketingCog(bot)

        class Category:
            overwrites = {}

        category = Category()
        created_channel = SimpleNamespace(
            mention="#gen-0002", send=AsyncMock()
        )
        guild = SimpleNamespace(
            id=123,
            channels=[SimpleNamespace(name="gen-0001")],
            roles=[SimpleNamespace(name="Support Staff", mention="<@&99>")],
            default_role=object(),
            me=None,
            get_channel=Mock(return_value=category),
            create_text_channel=AsyncMock(return_value=created_channel),
        )
        requester = Mock(mention="<@42>")
        interaction = SimpleNamespace(
            guild=guild,
            user=requester,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with patch("ticketing.discord.CategoryChannel", Category):
            await cog.create_ticket(interaction, "I need help")

        guild.create_text_channel.assert_awaited_once()
        channel_kwargs = guild.create_text_channel.await_args.kwargs
        self.assertEqual(channel_kwargs["name"], "gen-0002")
        self.assertIs(channel_kwargs["category"], category)
        message_kwargs = created_channel.send.await_args.kwargs
        self.assertIn("<@42> - Thank you for opening a ticket.", message_kwargs["content"])
        self.assertIn("@ Support Staff", message_kwargs["content"])
        self.assertNotIn("<@&99>", message_kwargs["content"])
        self.assertIn("General ticket", message_kwargs["embed"].description)
        self.assertEqual(message_kwargs["embed"].fields[0].name, "Question:")
        self.assertEqual(message_kwargs["embed"].fields[0].value, "*I need help*")

    async def test_whitelist_answer_is_posted_in_whitelist_ticket(self):
        bot = SimpleNamespace(user=object())
        config = '{"123": {"channel_id": 456}}'
        categories = '{"123": {"whitelist_submission": 987}}'
        with patch.dict(
            os.environ,
            {
                "TICKET_GUILD_CONFIGS": config,
                "TICKET_CATEGORY_CONFIGS": categories,
                "TICKET_STATE_PATH": os.devnull,
            },
        ):
            cog = TicketingCog(bot)

        class Category:
            name = "Whitelist"
            overwrites = {}

        category = Category()
        created_channel = SimpleNamespace(mention="#wls-0001", send=AsyncMock())
        guild = SimpleNamespace(
            id=123,
            channels=[],
            roles=[],
            default_role=object(),
            me=None,
            get_channel=Mock(return_value=category),
            create_text_channel=AsyncMock(return_value=created_channel),
        )
        requester = Mock(mention="<@42>")
        interaction = SimpleNamespace(
            guild=guild,
            user=requester,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with patch("ticketing.discord.CategoryChannel", Category):
            await cog.create_ticket(
                interaction, "Aster Vale", "whitelist_submission"
            )

        channel_kwargs = guild.create_text_channel.await_args.kwargs
        self.assertEqual(channel_kwargs["name"], "wls-0001")
        self.assertIs(channel_kwargs["category"], category)
        message_kwargs = created_channel.send.await_args.kwargs
        self.assertIn("Whitelist ticket", message_kwargs["embed"].description)
        self.assertEqual(
            message_kwargs["embed"].fields[0].name,
            "What is your Character's Name?",
        )
        self.assertEqual(message_kwargs["embed"].fields[0].value, "*Aster Vale*")


if __name__ == "__main__":
    unittest.main()
