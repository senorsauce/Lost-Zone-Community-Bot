import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from ticketing import (
    BugReportModal,
    GeneralSupportModal,
    TicketPanelView,
    TicketingCog,
    prepare_named_uploads,
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


class TicketUploadTests(unittest.TestCase):
    def test_bug_report_has_requested_questions_and_upload_field(self):
        modal = BugReportModal(SimpleNamespace())

        self.assertEqual(
            [item.text for item in modal.children[:-1]],
            [
                "How did this bug first occur?",
                "Did you lose anything as a result of the bug?",
                "Is this bug repeatable?",
                "Additional Comments? (Put N/A if no)",
            ],
        )
        self.assertEqual(modal.children[-1].text, "Upload files")
        self.assertEqual(modal.children[-1].component.max_values, 10)

    def test_upload_names_are_unique_and_keep_allowed_extension(self):
        named_uploads, error = prepare_named_uploads(
            "Server evidence\nreport.json",
            [
                {"original_name": "evidence.PNG", "data": b"image"},
                {"original_name": "upload.json", "data": b"{}"},
            ],
        )

        self.assertIsNone(error)
        self.assertEqual(
            named_uploads,
            [
                {"filename": "Server evidence.png", "data": b"image"},
                {"filename": "report.json", "data": b"{}"},
            ],
        )

    def test_rejects_unsupported_extensions_and_duplicate_names(self):
        unsupported, unsupported_error = prepare_named_uploads(
            "malware.exe", [{"original_name": "upload.exe", "data": b"x"}]
        )
        duplicates, duplicate_error = prepare_named_uploads(
            "evidence.jpg\nEVIDENCE.JPG",
            [
                {"original_name": "one.jpg", "data": b"1"},
                {"original_name": "two.jpg", "data": b"2"},
            ],
        )

        self.assertIsNone(unsupported)
        self.assertIn("Unsupported file format", unsupported_error)
        self.assertIsNone(duplicates)
        self.assertIn("unique name", duplicate_error)

    def test_requires_a_name_per_file_and_matching_extension(self):
        missing_name, missing_error = prepare_named_uploads(
            "", [{"original_name": "evidence.jpg", "data": b"x"}]
        )
        changed_extension, extension_error = prepare_named_uploads(
            "evidence.png", [{"original_name": "evidence.jpg", "data": b"x"}]
        )

        self.assertIsNone(missing_name)
        self.assertIn("exactly 1", missing_error)
        self.assertIsNone(changed_extension)
        self.assertIn("original accepted file extension", extension_error)


class TicketCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ticket_uses_category_and_sends_answers_with_uploads(self):
        with patch.dict(
            os.environ,
            {
                "TICKET_GUILD_CONFIGS": (
                    '{"123": {"channel_id": 456, "category_id": 789}}'
                ),
                "TICKET_STATE_PATH": os.devnull,
            },
        ):
            cog = TicketingCog(SimpleNamespace())

        class Category:
            overwrites = {}

        category = Category()
        ticket_channel = SimpleNamespace(
            mention="#bug-0001",
            send=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=123,
            default_role=Mock(),
            me=None,
            get_channel=Mock(return_value=category),
            create_text_channel=AsyncMock(return_value=ticket_channel),
        )
        interaction = SimpleNamespace(
            guild=guild,
            user=Mock(mention="<@42>"),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with patch("ticketing.discord.CategoryChannel", Category):
            await cog.create_ticket(
                interaction,
                {"How did this bug first occur?": "After restart"},
                "bug_report",
                [{"filename": "crash.txt", "data": b"log data"}],
            )

        create_kwargs = guild.create_text_channel.await_args.kwargs
        self.assertEqual(create_kwargs["name"], "bug-0001")
        self.assertIs(create_kwargs["category"], category)
        send_kwargs = ticket_channel.send.await_args.kwargs
        self.assertEqual(send_kwargs["embed"].fields[0].name, "How did this bug first occur?")
        self.assertEqual(send_kwargs["embed"].fields[0].value, "After restart")
        self.assertEqual([upload.filename for upload in send_kwargs["files"]], ["crash.txt"])
        self.assertEqual(send_kwargs["files"][0].fp.read(), b"log data")


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
        self.assertEqual(
            [button.style for button in posted["view"].children],
            [discord.ButtonStyle.secondary, discord.ButtonStyle.secondary],
        )
        self.assertEqual(posted["embed"].footer.text, "STALKER: Lost Zone Ticket Bot - Stalker Support")
        self.assertEqual(GeneralSupportModal(cog).children[0].text, "Describe your issue:")


if __name__ == "__main__":
    unittest.main()
