import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from ticketing import (
    BugReportModal,
    CloseRequestModal,
    EVIDENCE_ACKNOWLEDGMENT,
    GeneralSupportModal,
    TicketAcknowledgmentModal,
    TicketAcknowledgmentView,
    TicketSubmissionModal,
    TicketPanelView,
    TicketingCog,
    TICKET_TYPES,
    has_staff_role,
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


class TicketSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_submission_is_deferred_before_file_read(self):
        cog = SimpleNamespace(finish_ticket_submission=AsyncMock())
        modal = TicketSubmissionModal(cog, "general_support")
        modal.question_fields[0][1]._value = "Issue details"
        response = SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
        )
        attachment = SimpleNamespace(filename="evidence.txt")

        async def read_attachment():
            self.assertEqual(response.defer.await_count, 1)
            return b"evidence"

        attachment.read = AsyncMock(side_effect=read_attachment)
        modal.upload_field.component._values = [attachment]
        interaction = SimpleNamespace(
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await modal.on_submit(interaction)

        cog.finish_ticket_submission.assert_awaited_once_with(
            interaction,
            {"Describe your issue:": "Issue details"},
            "general_support",
            [{"original_name": "evidence.txt", "data": b"evidence"}],
        )


class TicketCreationTests(unittest.IsolatedAsyncioTestCase):
    async def _make_cog(self, ticket_categories):
        with patch.dict(
            os.environ,
            {
                "TICKET_GUILD_CONFIGS": '{"123": {"channel_id": 456}}',
                "TICKET_CATEGORY_CONFIGS": json.dumps({"123": ticket_categories}),
                "TICKET_STATE_PATH": os.devnull,
            },
        ):
            return TicketingCog(SimpleNamespace())

    async def _submit_ticket(self, cog, ticket_type, answers, uploads):
        class Category:
            overwrites = {}

        category = Category()
        ticket_channel = SimpleNamespace(
            mention=f"#{TICKET_TYPES[ticket_type]['channel_prefix']}-0001",
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
            user=Mock(id=42, mention="<@42>"),
            response=SimpleNamespace(
                defer=AsyncMock(),
                is_done=Mock(return_value=False),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with patch("ticketing.discord.CategoryChannel", Category):
            await cog.create_ticket(
                interaction,
                answers,
                ticket_type,
                uploads,
            )

        return guild, category, ticket_channel, interaction

    async def test_ticket_uses_category_and_sends_answers_with_uploads(self):
        cog = await self._make_cog({"bug_report": 789})
        guild, category, ticket_channel, _ = await self._submit_ticket(
            cog,
            "bug_report",
            {"How did this bug first occur?": "After restart"},
            [{"filename": "crash.txt", "data": b"log data"}],
        )

        create_kwargs = guild.create_text_channel.await_args.kwargs
        self.assertEqual(create_kwargs["name"], "bug-0001")
        self.assertIs(create_kwargs["category"], category)
        self.assertIn("ticket_owner_id=42", create_kwargs["topic"])
        opening_message, upload_message = ticket_channel.send.await_args_list
        opening_kwargs = opening_message.kwargs
        upload_kwargs = upload_message.kwargs
        self.assertEqual(
            opening_kwargs["content"],
            "Hello <@42> -- You have opened a Bug Report ticket. "
            "Please be patient while staff addresses your issue.",
        )
        embed = opening_kwargs["embed"]
        self.assertEqual(embed.fields[0].name, "How did this bug first occur?")
        self.assertEqual(embed.fields[0].value, "After restart")
        self.assertEqual(embed.footer.text, "STALKER: Lost Zone Ticket Bot - Stalker Support")
        self.assertTrue(embed.title)
        self.assertEqual(upload_kwargs["file"].filename, "crash.txt")
        self.assertEqual(upload_kwargs["file"].fp.read(), b"log data")

    async def test_all_non_whitelist_types_use_the_requested_greeting_and_embed(self):
        for ticket_type, config in TICKET_TYPES.items():
            if ticket_type == "whitelist_submission":
                continue
            with self.subTest(ticket_type=ticket_type):
                cog = await self._make_cog({ticket_type: 789})
                answers = {
                    question["label"]: "Answer"
                    for question in config["questions"]
                }
                if config.get("acknowledgment"):
                    answers[config["acknowledgment"]] = "Yes"
                _, _, ticket_channel, _ = await self._submit_ticket(
                    cog, ticket_type, answers, []
                )

                opening_kwargs = ticket_channel.send.await_args.kwargs
                self.assertEqual(
                    opening_kwargs["content"],
                    f"Hello <@42> -- You have opened a {config['label']} ticket. "
                    "Please be patient while staff addresses your issue.",
                )
                embed = opening_kwargs["embed"]
                self.assertEqual(
                    embed.description,
                    f"<@42> has opened a {config['label']} ticket.\n",
                )
                self.assertEqual(
                    [field.name for field in embed.fields[:len(config["questions"])]],
                    [question["label"] for question in config["questions"]],
                )

    async def test_whitelist_uses_its_custom_greeting(self):
        cog = await self._make_cog({"whitelist_submission": 789})
        _, _, ticket_channel, _ = await self._submit_ticket(
            cog,
            "whitelist_submission",
            {"What is your Character's Name?": "Stalker"},
            [],
        )

        opening_kwargs = ticket_channel.send.await_args.kwargs
        self.assertEqual(
            opening_kwargs["content"],
            "Welcome to STALKER: Lost Zone, <@42>! Your whitelist has been "
            "received, and the answers have been posted below for Staff review. "
            "Any corrections that need to be made by you will be requested here.\n\n"
            "Please be patient, thank you!",
        )
        self.assertEqual(
            opening_kwargs["embed"].fields[0].name,
            "What is your Character's Name?",
        )


class TicketPanelStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_sets_global_username_and_server_nickname(self):
        bot_user = SimpleNamespace(name="ZVTBOT", edit=AsyncMock())
        guild_member = SimpleNamespace(nick=None, edit=AsyncMock())
        guild = SimpleNamespace(id=123, me=guild_member)
        bot = SimpleNamespace(user=bot_user, guilds=[guild], add_view=Mock())

        with patch.dict(
            os.environ,
            {
                "BOT_USERNAME": "stalker_lost_zone",
                "BOT_NICKNAME": "STALKER: Lost Zone",
                "TICKET_GUILD_CONFIGS": "",
                "TICKET_STATE_PATH": os.devnull,
            },
        ):
            cog = TicketingCog(bot)

        await cog.on_ready()

        bot_user.edit.assert_awaited_once_with(username="stalker_lost_zone")
        guild_member.edit.assert_awaited_once_with(
            nick="STALKER: Lost Zone",
            reason="Set configured community bot nickname",
        )

    async def test_every_ticket_modal_completes_or_returns_acknowledgment(self):
        cog = SimpleNamespace(finish_ticket_submission=AsyncMock())

        for ticket_type in TICKET_TYPES:
            with self.subTest(ticket_type=ticket_type):
                cog.finish_ticket_submission.reset_mock()
                response = SimpleNamespace(defer=AsyncMock())
                followup = SimpleNamespace(send=AsyncMock())
                modal = TicketSubmissionModal(cog, ticket_type)

                await modal.on_submit(
                    SimpleNamespace(response=response, followup=followup)
                )

                response.defer.assert_awaited_once_with(
                    ephemeral=True, thinking=True
                )
                if TICKET_TYPES[ticket_type].get("acknowledgment"):
                    followup.send.assert_awaited_once()
                    self.assertIsInstance(
                        followup.send.await_args.kwargs["view"],
                        TicketAcknowledgmentView,
                    )
                else:
                    cog.finish_ticket_submission.assert_awaited_once()

    async def test_compensation_acknowledgment_uses_followup_and_button(self):
        cog = SimpleNamespace(finish_ticket_submission=AsyncMock())
        modal = TicketSubmissionModal(cog, "compensation_request")
        response = SimpleNamespace(
            defer=AsyncMock(),
            is_done=Mock(return_value=True),
            send_modal=AsyncMock(),
        )
        followup = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(response=response, followup=followup)

        await modal.on_submit(interaction)

        response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        followup.send.assert_awaited_once()
        acknowledgment_view = followup.send.await_args.kwargs["view"]
        button_response = SimpleNamespace(send_modal=AsyncMock())
        await acknowledgment_view.children[0].callback(
            SimpleNamespace(response=button_response)
        )

        acknowledgment_modal = button_response.send_modal.await_args.args[0]
        self.assertIsInstance(acknowledgment_modal, TicketAcknowledgmentModal)
        self.assertEqual(
            acknowledgment_modal.children[0].content,
            EVIDENCE_ACKNOWLEDGMENT,
        )

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
                for index, (question, _) in enumerate(modal.question_fields):
                    label = modal.children[index]
                    self.assertLessEqual(len(label.text), 45)
                    rendered_question = label.text
                    if label.description:
                        rendered_question += f" {label.description}"
                    self.assertEqual(rendered_question, question)
                self.assertIsInstance(modal.children[-1].component, discord.ui.FileUpload)
                modal.to_dict()

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


class StaffTicketCommandTests(unittest.IsolatedAsyncioTestCase):
    async def _make_cog(self, bot=None):
        with patch.dict(
            os.environ,
            {
                "TICKET_GUILD_CONFIGS": "",
                "TICKET_STATE_PATH": os.devnull,
            },
        ):
            return TicketingCog(bot or SimpleNamespace(add_view=Mock()))

    async def test_staff_role_gate_is_case_insensitive(self):
        staff_interaction = SimpleNamespace(
            user=SimpleNamespace(roles=[SimpleNamespace(name="sTaFf")])
        )
        other_interaction = SimpleNamespace(
            user=SimpleNamespace(roles=[SimpleNamespace(name="Moderator")])
        )

        self.assertTrue(await has_staff_role(staff_interaction))
        self.assertFalse(await has_staff_role(other_interaction))
        for command in (TicketingCog.useradd, TicketingCog.userremove, TicketingCog.closerequest):
            self.assertIn(has_staff_role, command.checks)

    async def test_useradd_and_userremove_update_ticket_overwrites(self):
        cog = await self._make_cog()
        member = SimpleNamespace(id=22, mention="<@22>")

        class Channel:
            name = "gen-0001"
            topic = "ticket_type=general_support;ticket_owner_id=11"

            def __init__(self):
                self.overwrite = discord.PermissionOverwrite()
                self.set_permissions = AsyncMock()

            def overwrites_for(self, target):
                return self.overwrite

        channel = Channel()

        def interaction():
            return SimpleNamespace(
                channel=channel,
                user=SimpleNamespace(roles=[SimpleNamespace(name="Staff")]),
                response=SimpleNamespace(
                    is_done=Mock(return_value=False), send_message=AsyncMock()
                ),
                followup=SimpleNamespace(send=AsyncMock()),
            )

        with patch("ticketing.discord.TextChannel", Channel):
            await TicketingCog.useradd.callback(cog, interaction(), member)
            self.assertTrue(channel.overwrite.view_channel)
            self.assertTrue(channel.overwrite.send_messages)
            self.assertTrue(channel.overwrite.read_message_history)
            await TicketingCog.userremove.callback(cog, interaction(), member)

        self.assertEqual(channel.set_permissions.await_count, 2)
        removed_overwrite = channel.set_permissions.await_args.kwargs["overwrite"]
        self.assertFalse(removed_overwrite.view_channel)
        self.assertFalse(removed_overwrite.send_messages)

    async def test_close_request_posts_named_owner_staff_reason_and_button(self):
        bot = SimpleNamespace(add_view=Mock())
        cog = await self._make_cog(bot)

        class Channel:
            id = 55
            name = "cmp-0001"
            topic = "ticket_type=compensation_request;ticket_owner_id=11"
            guild = SimpleNamespace(
                roles=[SimpleNamespace(name="Staff", mention="<@&33>")]
            )
            send = AsyncMock(return_value=SimpleNamespace(id=88))

        channel = Channel()
        interaction = SimpleNamespace(
            channel=channel,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with patch("ticketing.discord.TextChannel", Channel):
            await cog.post_close_request(interaction, "No longer needed")

        send_kwargs = channel.send.await_args.kwargs
        self.assertEqual(
            send_kwargs["embed"].description,
            "<@11>, <@&33> has requested to close this ticket with reason : "
            "No longer needed. Click below to accept.",
        )
        self.assertEqual(send_kwargs["view"].children[0].label, "Accept")
        bot.add_view.assert_called_once_with(send_kwargs["view"], message_id=88)
        self.assertEqual(CloseRequestModal(cog).reason.text, "Reason for closing")

    async def test_accepting_close_request_locks_ticket_but_preserves_history(self):
        cog = await self._make_cog()
        channel = SimpleNamespace(
            id=55,
            topic="ticket_type=general_support;ticket_owner_id=11",
            overwrites_for=Mock(return_value=discord.PermissionOverwrite()),
            set_permissions=AsyncMock(),
            edit=AsyncMock(),
        )
        request_message = SimpleNamespace(
            embeds=[discord.Embed(title="Ticket closure request")],
            edit=AsyncMock(),
        )
        interaction = SimpleNamespace(
            channel=channel,
            user=SimpleNamespace(id=11),
            message=request_message,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await cog.accept_close_request(interaction, 55, 11)

        overwrite = channel.set_permissions.await_args.kwargs["overwrite"]
        self.assertTrue(overwrite.view_channel)
        self.assertTrue(overwrite.read_message_history)
        self.assertFalse(overwrite.send_messages)
        self.assertIn("ticket_status=closed", channel.edit.await_args.kwargs["topic"])
        request_message.edit.assert_awaited_once()
        self.assertIsNone(request_message.edit.await_args.kwargs["view"])

    async def test_only_ticket_opener_can_accept_close_request(self):
        cog = await self._make_cog()
        channel = SimpleNamespace(id=55)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            channel=channel,
            user=SimpleNamespace(id=99),
            response=response,
        )

        await cog.accept_close_request(interaction, channel_id=55, owner_id=11)

        response.send_message.assert_awaited_once()
        self.assertFalse(hasattr(channel, "set_permissions"))


if __name__ == "__main__":
    unittest.main()
