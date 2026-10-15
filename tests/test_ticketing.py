import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ticketing import (
    GeneralSupportModal,
    TicketPanelView,
    TicketingCog,
    parse_ticket_guild_configs,
)


class TicketGuildConfigTests(unittest.TestCase):
    def test_parses_per_guild_channel_and_optional_category(self):
        configs = parse_ticket_guild_configs(
            '{"123": {"channel_id": "456", "category_id": "789"}, '
            '"234": {"channel_id": 567}}'
        )

        self.assertEqual(
            configs,
            {"123": {"channel_id": 456, "category_id": 789}, "234": {"channel_id": 567}},
        )

    def test_rejects_missing_panel_channel(self):
        with self.assertRaisesRegex(RuntimeError, "must include a channel_id"):
            parse_ticket_guild_configs('{"123": {"category_id": 789}}')


class TicketPanelStartupTests(unittest.IsolatedAsyncioTestCase):
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

        self.assertEqual(posted["embed"].title, "Ticket types:")
        self.assertIsNone(posted["view"].timeout)
        self.assertEqual(posted["view"].children[0].label, "General Support")
        self.assertEqual(GeneralSupportModal(cog).children[0].text, "Describe your issue:")


if __name__ == "__main__":
    unittest.main()
