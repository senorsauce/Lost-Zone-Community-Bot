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
    parse_ticket_category_configs,
    parse_ticket_guild_configs,
    validate_upload_extension,
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

    def test_parses_per_ticket_categories(self):
        configs = parse_ticket_category_configs(
            '{"123": {"general_support": "789", "bug_report": 987}}'
        )

        self.assertEqual(configs, {"123": {"general_support": 789, "bug_report": 987}})

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

    def test_upload_extension_validation_accepts_only_supported_formats(self):
        for extension in (".jpg", ".png", ".json", ".zip", ".txt", ".mp3", ".mp4"):
            with self.subTest(extension=extension):
                self.assertTrue(validate_upload_extension(f"evidence{extension}"))

        for filename in ("evidence.exe", "no-extension", "evidence.jpg.exe"):
            with self.subTest(filename=filename):
                self.assertFalse(validate_upload_extension(filename))


class TicketCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ticket_uses_category_and_sends_answers_with_uploads(self):
        with patch.dict(
            os.environ,
            {
                "TICKET_GUILD_CONFIGS": (
                    '{"123": {"channel_id": 456}}'
                ),
                "TICKET_CATEGORY_CONFIGS": '{"123": {"bug_report": 789}}',
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
            channels=[],
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
        self.assertEqual(send_kwargs["embed"].fields[0].value, "*After restart*")
        self.assertEqual([upload.filename for upload in send_kwargs["files"]], ["crash.txt"])
        self.assertEqual(send_kwargs["files"][0].fp.read(), b"log data")
        self.assertEqual(send_kwargs["embed"].footer.text, "STALKER: Lost Zone Ticket Bot - Stalker Support")
        self.assertTrue(send_kwargs["embed"].title)


class TicketPanelStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_ticket_button_opens_a_valid_upload_modal(self):
        bot = SimpleNamespace(add_view=Mock())
        with patch.dict(os.environ, {"TICKET_STATE_PATH": os.devnull}):
            cog = TicketingCog(bot)
        view = TicketPanelView(cog, 123)

        self.assertEqual(len(view.children), 7)
        for button in view.children:
            with self.subTest(ticket=button.label):
                response = SimpleNamespace(send_modal=AsyncMock())
                await button.callback(SimpleNamespace(response=response))

                modal = response.send_modal.await_args.args[0]
                self.assertIsInstance(modal, discord.ui.Modal)
                self.assertLessEqual(len(modal.children), 5)
                self.assertIsInstance(modal.children[-1].component, discord.ui.FileUpload)

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
            [
                "General Support",
                "Whitelist Submission",
                "Warfare",
                "Rulebreak Report",
                "Admin Report",
                "Bug Report",
                "Compensation Request",
            ],
        )
        self.assertEqual(
            [button.style for button in posted["view"].children],
            [discord.ButtonStyle.secondary] * 7,
        )
        self.assertEqual(posted["view"].children[0].emoji.name, "❓")
        self.assertEqual(posted["view"].children[5].emoji.name, "🪲")
        self.assertEqual(posted["embed"].footer.text, "STALKER: Lost Zone Ticket Bot - Stalker Support")
        self.assertEqual(GeneralSupportModal(cog).children[0].text, "Describe your issue:")
        self.assertEqual(
            [item.text for item in BugReportModal(cog).children[:-1]],
            [
                "How did this bug first occur?",
                "Did you lose anything as a result of the bug?",
                "Is this bug repeatable?",
                "Additional Comments? (Put N/A if no)",
            ],
        )


if __name__ == "__main__":
    unittest.main()
