import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from suggestions import (
    ALLOWED_REACTIONS,
    SuggestionCog,
)


class SuggestionCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_rejects_submissions_outside_configured_channel(self):
        cog = SuggestionCog(SimpleNamespace())
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            channel_id=123,
        )

        await cog.suggest.callback(cog, interaction, "Suggestion")

        interaction.followup.send.assert_awaited_once()
        self.assertIn("1531432683702456411", interaction.followup.send.await_args.args[0])

    async def test_posts_numbered_suggestion_and_adds_only_two_reactions(self):
        existing_embed = discord.Embed(title="Suggestion #07")
        existing_message = SimpleNamespace(
            author="bot-user",
            embeds=[existing_embed],
        )
        sent_message = SimpleNamespace(id=20, add_reaction=AsyncMock())

        class Channel:
            guild = SimpleNamespace(id=10)

            async def history(self, limit, oldest_first):
                yield existing_message

            send = AsyncMock(return_value=sent_message)

        channel = Channel()
        bot = SimpleNamespace(
            user="bot-user",
            get_channel=Mock(return_value=channel),
        )
        cog = SuggestionCog(bot)
        response = SimpleNamespace(defer=AsyncMock())
        followup = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            response=response,
            followup=followup,
            guild_id=10,
            channel_id=1531432683702456411,
            user=SimpleNamespace(id=11),
        )

        with patch("suggestions.discord.TextChannel", Channel):
            await cog.suggest.callback(cog, interaction, "Add a community event")

        embed = channel.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "Suggestion #08")
        self.assertEqual(embed.description, "Add a community event")
        self.assertIsNone(embed.footer.text)
        self.assertEqual(
            [call.args[0] for call in sent_message.add_reaction.await_args_list],
            ["✅", "❌"],
        )
        self.assertEqual(ALLOWED_REACTIONS, {"✅", "❌"})

    async def test_removes_other_reactions_from_suggestion_messages(self):
        suggestion = SimpleNamespace(
            author=SimpleNamespace(id=99),
            embeds=[discord.Embed(title="Suggestion #0001")],
            remove_reaction=AsyncMock(),
        )
        member = SimpleNamespace(id=11)
        channel = SimpleNamespace(
            guild=SimpleNamespace(get_member=Mock(return_value=member)),
            fetch_message=AsyncMock(return_value=suggestion),
        )
        bot = SimpleNamespace(
            user=suggestion.author,
            get_channel=Mock(return_value=channel),
        )
        cog = SuggestionCog(bot)
        payload = SimpleNamespace(
            channel_id=1531432683702456411,
            message_id=22,
            user_id=11,
            emoji=discord.PartialEmoji(name="🔥"),
            member=member,
            cached_message=None,
        )

        with patch("suggestions.discord.TextChannel", SimpleNamespace):
            await cog.on_raw_reaction_add(payload)

        suggestion.remove_reaction.assert_awaited_once_with(payload.emoji, member)

    async def test_keeps_allowed_reactions_and_ignores_other_messages(self):
        channel = SimpleNamespace(fetch_message=AsyncMock())
        cog = SuggestionCog(
            SimpleNamespace(user=SimpleNamespace(id=99), get_channel=Mock(return_value=channel))
        )
        for emoji in ("✅", "❌"):
            payload = SimpleNamespace(
                channel_id=1531432683702456411,
                user_id=11,
                emoji=discord.PartialEmoji(name=emoji),
            )
            await cog.on_raw_reaction_add(payload)
        channel.fetch_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()