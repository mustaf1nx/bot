import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from telegram.error import NetworkError, TelegramError, TimedOut
from bot import (
    AdminRegistry,
    OPRegistry,
    build_welcome_text,
    format_admin_tag,
    handle_op_message,
    is_connect_timeout,
    is_connection_error,
    log_error,
    parse_id_list,
    reply_with_connect_retry,
    swap_keyboard_layout,
)


class AdminRegistryTests(unittest.TestCase):
    def test_parse_id_list(self) -> None:
        self.assertEqual(parse_id_list("123, 456,123"), {123, 456})

    def test_admin_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admins.json"
            registry = AdminRegistry(path)
            self.assertTrue(registry.add(123))
            self.assertFalse(registry.add(123))
            self.assertTrue(AdminRegistry(path).contains(123))


class OPRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_default_ops_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            ops = registry.get_all()
            self.assertEqual(len(ops), 17)
            self.assertIn("SE", ops)
            self.assertIn("IT", ops)
            self.assertIn("BDA", ops)
            self.assertIn("CS", ops)
            self.assertEqual(ops["SE"].school, "School of Software Engineering")

    def test_find_matching_ops_short_code_and_full_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)

            # Match code
            matches_se = registry.find_matching_ops("Привет, я поступаю на SE!")
            self.assertEqual([op.code for op in matches_se], ["SE"])

            # Match full name
            matches_bda = registry.find_matching_ops("Что насчет Big Data Analysis?")
            self.assertEqual([op.code for op in matches_bda], ["BDA"])

            # Multiple matches
            matches_multi = registry.find_matching_ops("Выбираю между SE и Cybersecurity")
            matched_codes = {op.code for op in matches_multi}
            self.assertEqual(matched_codes, {"SE", "CS"})

    def test_find_matching_ops_avoids_false_positives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)

            # 'reset' should not match 'SE', 'with' should not match 'IT'
            matches = registry.find_matching_ops("Please reset my password with new suite")
            self.assertEqual(len(matches), 0)

    def test_set_admin_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            self.assertTrue(registry.set_admin("SE", "@new_se_admin"))
            self.assertEqual(registry.get("SE").admin, "@new_se_admin")

            # Reload from disk
            reloaded = OPRegistry(path)
            self.assertEqual(reloaded.get("SE").admin, "@new_se_admin")

    def test_format_admin_tag(self) -> None:
        self.assertEqual(format_admin_tag("@alex"), "@alex")
        self.assertEqual(format_admin_tag("alex"), "@alex")
        self.assertEqual(
            format_admin_tag("950705809"),
            '<a href="tg://user?id=950705809">Администратор</a>',
        )

    def test_find_matching_ops_aliases_and_cyrillic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)

            # Match Cyrillic slang
            matches_seshnik = registry.find_matching_ops("Я сешник")
            self.assertEqual([op.code for op in matches_seshnik], ["SE"])

            matches_aitishnik = registry.find_matching_ops("Привет я айтишник")
            self.assertEqual([op.code for op in matches_aitishnik], ["IT"])

            matches_kiberbez = registry.find_matching_ops("выбрал кибербез")
            self.assertEqual([op.code for op in matches_kiberbez], ["CS"])

            matches_bdashnik = registry.find_matching_ops("бдашник тут")
            self.assertEqual([op.code for op in matches_bdashnik], ["BDA"])

    async def test_handle_op_message_triggers_only_for_target_member(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=5)

            chat_id = 100
            welcome_msg_id = 555
            new_user_id = 123
            old_user_id = 456

            tracker.add_welcome_message(chat_id, welcome_msg_id, new_user_id)

            context = MagicMock()
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            # Old user (amer) replies to welcome message meant for cHezHrr -> IGNORED
            update_old_user = MagicMock()
            update_old_user.effective_message.chat.id = chat_id
            update_old_user.effective_message.from_user.id = old_user_id
            update_old_user.effective_message.from_user.is_bot = False
            update_old_user.effective_message.reply_to_message.message_id = welcome_msg_id
            update_old_user.effective_message.text = "Software engineering"
            update_old_user.effective_message.reply_text = AsyncMock()

            await handle_op_message(update_old_user, context)
            update_old_user.effective_message.reply_text.assert_not_called()

            # Target new user (cHezHrr) replies to welcome message -> MATCHED
            update_new_user = MagicMock()
            update_new_user.effective_message.chat.id = chat_id
            update_new_user.effective_message.from_user.id = new_user_id
            update_new_user.effective_message.from_user.is_bot = False
            update_new_user.effective_message.from_user.mention_html.return_value = "@cHezHrr"
            update_new_user.effective_message.reply_to_message.message_id = welcome_msg_id
            update_new_user.effective_message.text = "Electronic Engineering"
            update_new_user.effective_message.reply_text = AsyncMock()

            await handle_op_message(update_new_user, context)
            update_new_user.effective_message.reply_text.assert_called_once()
            called_text = update_new_user.effective_message.reply_text.call_args[0][0]
            self.assertIn("EE", called_text)
            self.assertIn("@dhshrbrhr", called_text)

    async def test_question_patterns_are_ignored(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=5)
            tracker.add_user(100, 123)

            context = MagicMock()
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            for question_text in ["Привет кто с се?", "Есть кто с ИТ?", "Кто тут на BDA?"]:
                update_q = MagicMock()
                update_q.effective_message.chat.id = 100
                update_q.effective_message.from_user.id = 123
                update_q.effective_message.from_user.is_bot = False
                update_q.effective_message.reply_to_message = None
                update_q.effective_message.text = question_text
                update_q.effective_message.reply_text = AsyncMock()

                await handle_op_message(update_q, context)
                update_q.effective_message.reply_text.assert_not_called()

    def test_swap_keyboard_layout(self) -> None:
        self.assertEqual(swap_keyboard_layout("Ct"), "Се")
        self.assertEqual(swap_keyboard_layout("CT"), "СЕ")
        self.assertEqual(swap_keyboard_layout("ct"), "се")
        self.assertEqual(swap_keyboard_layout("ыу"), "se")
        self.assertEqual(swap_keyboard_layout("vrc"), "мкс")

    def test_find_matching_ops_keyboard_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)

            matches_ct = registry.find_matching_ops("Ct")
            self.assertEqual([op.code for op in matches_ct], ["SE"])

            matches_ct_upper = registry.find_matching_ops("CT")
            self.assertEqual([op.code for op in matches_ct_upper], ["SE"])

            matches_se_layout = registry.find_matching_ops("ыу")
            self.assertEqual([op.code for op in matches_se_layout], ["SE"])

            matches_mcs_layout = registry.find_matching_ops("vrc")
            self.assertEqual([op.code for op in matches_mcs_layout], ["MCS"])

    async def test_handle_op_message_unrecognized_reply_to_bot(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=10)

            bot_id = 999
            context = MagicMock()
            context.bot.id = bot_id
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            tracker.add_welcome_message(100, 500, 777)

            update_reply = MagicMock()
            update_reply.effective_message.chat.id = 100
            update_reply.effective_message.from_user.id = 777
            update_reply.effective_message.from_user.is_bot = False
            update_reply.effective_message.from_user.mention_html.return_value = "@student"
            update_reply.effective_message.reply_to_message.message_id = 500
            update_reply.effective_message.reply_to_message.from_user.id = bot_id
            update_reply.effective_message.reply_to_message.text = "Какое у тебя оп?"
            update_reply.effective_message.text = "не знаю какая у меня оп"
            update_reply.effective_message.reply_text = AsyncMock()

            await handle_op_message(update_reply, context)
            update_reply.effective_message.reply_text.assert_called_once()
            called_text = update_reply.effective_message.reply_text.call_args[0][0]
            self.assertIn("Не удалось распознать ОП", called_text)
            self.assertIn("/ops", called_text)

    async def test_handle_op_message_ignores_replies_to_non_welcome_bot_messages(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=10)

            bot_id = 999
            context = MagicMock()
            context.bot.id = bot_id
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            update_vi = MagicMock()
            update_vi.effective_message.chat.id = 100
            update_vi.effective_message.from_user.id = 888
            update_vi.effective_message.from_user.is_bot = False
            update_vi.effective_message.from_user.mention_html.return_value = "ви"
            update_vi.effective_message.reply_to_message.message_id = 600
            update_vi.effective_message.reply_to_message.from_user.id = bot_id
            update_vi.effective_message.reply_to_message.text = "Привет, 🪿! 👋 📍 ОП: SE (Software Engineering)"
            update_vi.effective_message.text = "Привет диас"
            update_vi.effective_message.reply_text = AsyncMock()

            await handle_op_message(update_vi, context)
            update_vi.effective_message.reply_text.assert_not_called()

    async def test_handle_op_message_asks_cs_clarification(self) -> None:
        from bot import WelcomeTracker

        async def run_clarification(registry: OPRegistry, tracker: WelcomeTracker, answer: str):
            chat_id, welcome_msg_id, user_id = 100, 500, 777
            tracker.add_welcome_message(chat_id, welcome_msg_id, user_id)
            context = MagicMock()
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            # Reply "CS" -> clarification question, not an admin answer
            update_cs = MagicMock()
            update_cs.effective_message.chat.id = chat_id
            update_cs.effective_message.from_user.id = user_id
            update_cs.effective_message.from_user.is_bot = False
            update_cs.effective_message.from_user.mention_html.return_value = "@student"
            update_cs.effective_message.reply_to_message.message_id = welcome_msg_id
            update_cs.effective_message.text = "CS"
            update_cs.effective_message.reply_text = AsyncMock()

            await handle_op_message(update_cs, context)
            update_cs.effective_message.reply_text.assert_called_once()
            called_text = update_cs.effective_message.reply_text.call_args[0][0]
            self.assertIn("Computer Science (IT)", called_text)
            self.assertIn("Cybersecurity (CS)", called_text)

            clarification_id = update_cs.effective_message.reply_text.return_value.message_id

            # Answer the clarification
            update_answer = MagicMock()
            update_answer.effective_message.chat.id = chat_id
            update_answer.effective_message.from_user.id = user_id
            update_answer.effective_message.from_user.is_bot = False
            update_answer.effective_message.from_user.mention_html.return_value = "@student"
            update_answer.effective_message.reply_to_message.message_id = clarification_id
            update_answer.effective_message.text = answer
            update_answer.effective_message.reply_text = AsyncMock()

            await handle_op_message(update_answer, context)
            return update_answer.effective_message.reply_text.call_args[0][0]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)

            # Full name -> IT program
            tracker = WelcomeTracker(max_messages=10)
            text = await run_clarification(registry, tracker, "Computer Science")
            self.assertIn("IT", text)
            self.assertIn("@TypicallyRain", text)

            # Code IT -> IT program
            tracker = WelcomeTracker(max_messages=10)
            text = await run_clarification(registry, tracker, "IT")
            self.assertIn("IT", text)
            self.assertIn("@TypicallyRain", text)

            # Code CS -> Cybersecurity program
            tracker = WelcomeTracker(max_messages=10)
            text = await run_clarification(registry, tracker, "CS")
            self.assertIn("CS", text)
            self.assertIn("@alishaisyapping", text)

            # Full name -> Cybersecurity program
            tracker = WelcomeTracker(max_messages=10)
            text = await run_clarification(registry, tracker, "Cybersecurity")
            self.assertIn("CS", text)
            self.assertIn("@alishaisyapping", text)

    def test_build_welcome_text_contains_reply_instruction(self) -> None:
        mock_chain = MagicMock()
        mock_chain.generate.return_value = "Приветственный текст"
        welcome_text = build_welcome_text(mock_chain, "@user", 28)
        self.assertIn("Ответь на это сообщение (Reply)", welcome_text)




class NetworkErrorHandlingTests(unittest.IsolatedAsyncioTestCase):
    def test_is_connection_error_detection(self) -> None:
        class DummyConnectError(Exception):
            pass

        class DummyRemoteProtocolError(Exception):
            pass

        connect_err = NetworkError("Connect failed")
        connect_err.__cause__ = DummyConnectError("No address associated with hostname")
        self.assertTrue(is_connection_error(connect_err))
        self.assertTrue(is_connect_timeout(connect_err))

        remote_err = NetworkError("Server disconnected")
        remote_err.__cause__ = DummyRemoteProtocolError("Server disconnected without sending a response")
        self.assertTrue(is_connection_error(remote_err))

        timeout_err = TimedOut("Timed out")
        self.assertTrue(is_connection_error(timeout_err))

        val_err = ValueError("Invalid parameter")
        self.assertFalse(is_connection_error(val_err))

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_reply_with_connect_retry_recovers(self, mock_sleep: AsyncMock) -> None:
        mock_message = MagicMock()
        mock_message.reply_text = AsyncMock(
            side_effect=[
                NetworkError("httpx.ConnectError: [Errno -5] No address associated with hostname"),
                None,
            ]
        )
        await reply_with_connect_retry(mock_message, "Hello")
        self.assertEqual(mock_message.reply_text.call_count, 2)
        mock_sleep.assert_called_once_with(1.0)

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_reply_with_connect_retry_fails_after_max_attempts(
        self, mock_sleep: AsyncMock
    ) -> None:
        mock_message = MagicMock()
        mock_message.reply_text = AsyncMock(
            side_effect=NetworkError("httpx.RemoteProtocolError: Server disconnected")
        )
        with self.assertRaises(NetworkError):
            await reply_with_connect_retry(mock_message, "Hello")
        self.assertEqual(mock_message.reply_text.call_count, 3)

    @patch("bot.LOGGER")
    async def test_log_error_formatting(self, mock_logger: MagicMock) -> None:
        context = MagicMock()
        context.error = NetworkError("Server disconnected")
        await log_error(None, context)
        mock_logger.warning.assert_called_once()
        mock_logger.error.assert_not_called()

        mock_logger.reset_mock()
        context.error = ValueError("Something broke")
        await log_error(None, context)
        mock_logger.error.assert_called_once()
        mock_logger.warning.assert_not_called()


class NewcomerDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_newcomer_unreplied_standalone_op(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=5)
            chat_id = 100
            new_user_id = 123

            tracker.add_user(chat_id, new_user_id)

            context = MagicMock()
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            update = MagicMock()
            update.effective_message.chat.id = chat_id
            update.effective_message.from_user.id = new_user_id
            update.effective_message.from_user.is_bot = False
            update.effective_message.from_user.mention_html.return_value = "@newbie"
            update.effective_message.reply_to_message = None
            update.effective_message.text = "SE"
            update.effective_message.reply_text = AsyncMock()

            await handle_op_message(update, context)
            update.effective_message.reply_text.assert_called_once()
            called_text = update.effective_message.reply_text.call_args[0][0]
            self.assertIn("SE", called_text)
            self.assertIn("@Alsh444", called_text)
            self.assertFalse(tracker.is_active_newcomer(chat_id, new_user_id))

    async def test_newcomer_unreplied_self_id_phrases(self) -> None:
        from bot import WelcomeTracker

        phrases = [
            ("я на се", "SE"),
            ("я с се", "SE"),
            ("поступил на программную инженерию", "SE"),
            ("привет я айтишник", "IT"),
            ("моя оп BDA", "BDA"),
            ("выбрал кибербез", "CS"),
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)

            for phrase, expected_op in phrases:
                tracker = WelcomeTracker(max_messages=5)
                chat_id = 100
                user_id = 200
                tracker.add_user(chat_id, user_id)

                context = MagicMock()
                context.application.bot_data = {
                    "op_registry": registry,
                    "welcome_tracker": tracker,
                }

                update = MagicMock()
                update.effective_message.chat.id = chat_id
                update.effective_message.from_user.id = user_id
                update.effective_message.from_user.is_bot = False
                update.effective_message.from_user.mention_html.return_value = "@user"
                update.effective_message.reply_to_message = None
                update.effective_message.text = phrase
                update.effective_message.reply_text = AsyncMock()

                await handle_op_message(update, context)
                update.effective_message.reply_text.assert_called_once()
                called_text = update.effective_message.reply_text.call_args[0][0]
                self.assertIn(expected_op, called_text)

    async def test_newcomer_general_chat_ignored_silently(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=5)
            chat_id = 100
            user_id = 123
            tracker.add_user(chat_id, user_id)

            context = MagicMock()
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            for text in ["Всем привет!", "Привет, народ!", "Подскажите правила группы"]:
                update = MagicMock()
                update.effective_message.chat.id = chat_id
                update.effective_message.from_user.id = user_id
                update.effective_message.from_user.is_bot = False
                update.effective_message.reply_to_message = None
                update.effective_message.text = text
                update.effective_message.reply_text = AsyncMock()

                await handle_op_message(update, context)
                update.effective_message.reply_text.assert_not_called()

            self.assertEqual(tracker._active_new_members[(chat_id, user_id)]["messages_left"], 2)

    async def test_old_member_unreplied_ignored_completely(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=5)

            context = MagicMock()
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            update = MagicMock()
            update.effective_message.chat.id = 100
            update.effective_message.from_user.id = 999
            update.effective_message.from_user.is_bot = False
            update.effective_message.reply_to_message = None
            update.effective_message.text = "SE"
            update.effective_message.reply_text = AsyncMock()

            await handle_op_message(update, context)
            update.effective_message.reply_text.assert_not_called()

    async def test_long_sentence_with_short_code_ignored_without_self_id(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=5)
            chat_id = 100
            user_id = 123
            tracker.add_user(chat_id, user_id)

            context = MagicMock()
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            update = MagicMock()
            update.effective_message.chat.id = chat_id
            update.effective_message.from_user.id = user_id
            update.effective_message.from_user.is_bot = False
            update.effective_message.reply_to_message = None
            update.effective_message.text = "я се вчера купил новый мощный ноутбук для учебы"
            update.effective_message.reply_text = AsyncMock()

            await handle_op_message(update, context)
            update.effective_message.reply_text.assert_not_called()

    async def test_newcomer_tracking_expiration_by_messages(self) -> None:
        from bot import WelcomeTracker

        tracker = WelcomeTracker(max_messages=3)
        chat_id = 100
        user_id = 123
        tracker.add_user(chat_id, user_id)
        self.assertTrue(tracker.is_active_newcomer(chat_id, user_id))

        tracker.record_message(chat_id, user_id)
        self.assertTrue(tracker.is_active_newcomer(chat_id, user_id))
        tracker.record_message(chat_id, user_id)
        self.assertTrue(tracker.is_active_newcomer(chat_id, user_id))
        tracker.record_message(chat_id, user_id)
        self.assertFalse(tracker.is_active_newcomer(chat_id, user_id))

    async def test_newcomer_tracking_expiration_by_ttl(self) -> None:
        import time
        from bot import WelcomeTracker

        tracker = WelcomeTracker(max_messages=5, ttl_seconds=0.05)
        chat_id = 100
        user_id = 123
        tracker.add_user(chat_id, user_id)
        self.assertTrue(tracker.is_active_newcomer(chat_id, user_id))

        time.sleep(0.1)
        self.assertFalse(tracker.is_active_newcomer(chat_id, user_id))

    async def test_newcomer_unreplied_cs_triggers_clarification_and_resolves(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=5)
            chat_id = 100
            user_id = 123
            tracker.add_user(chat_id, user_id)

            context = MagicMock()
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            # 1. Newcomer sends "CS" in chat without Reply
            update_cs = MagicMock()
            update_cs.effective_message.chat.id = chat_id
            update_cs.effective_message.from_user.id = user_id
            update_cs.effective_message.from_user.is_bot = False
            update_cs.effective_message.from_user.mention_html.return_value = "@student"
            update_cs.effective_message.reply_to_message = None
            update_cs.effective_message.text = "CS"
            update_cs.effective_message.reply_text = AsyncMock()

            await handle_op_message(update_cs, context)
            update_cs.effective_message.reply_text.assert_called_once()
            called_text = update_cs.effective_message.reply_text.call_args[0][0]
            self.assertIn("Computer Science (IT)", called_text)
            self.assertIn("Cybersecurity (CS)", called_text)
            self.assertTrue(tracker.is_pending_clarification(chat_id, user_id))

            # 2. Newcomer answers "IT" without Reply
            update_answer = MagicMock()
            update_answer.effective_message.chat.id = chat_id
            update_answer.effective_message.from_user.id = user_id
            update_answer.effective_message.from_user.is_bot = False
            update_answer.effective_message.from_user.mention_html.return_value = "@student"
            update_answer.effective_message.reply_to_message = None
            update_answer.effective_message.text = "IT"
            update_answer.effective_message.reply_text = AsyncMock()

            await handle_op_message(update_answer, context)
            update_answer.effective_message.reply_text.assert_called_once()
            ans_text = update_answer.effective_message.reply_text.call_args[0][0]
            self.assertIn("IT", ans_text)
            self.assertIn("@TypicallyRain", ans_text)
            self.assertFalse(tracker.is_pending_clarification(chat_id, user_id))
            self.assertFalse(tracker.is_active_newcomer(chat_id, user_id))

    async def test_newcomer_unreplied_keyboard_layout_translit(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=5)
            chat_id = 100
            user_id = 123
            tracker.add_user(chat_id, user_id)

            context = MagicMock()
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            # "ыу" is SE on Russian keyboard
            update = MagicMock()
            update.effective_message.chat.id = chat_id
            update.effective_message.from_user.id = user_id
            update.effective_message.from_user.is_bot = False
            update.effective_message.from_user.mention_html.return_value = "@user"
            update.effective_message.reply_to_message = None
            update.effective_message.text = "ыу"
            update.effective_message.reply_text = AsyncMock()

            await handle_op_message(update, context)
            update.effective_message.reply_text.assert_called_once()
            called_text = update.effective_message.reply_text.call_args[0][0]
            self.assertIn("SE", called_text)

    async def test_newcomer_question_variations_stay_silent(self) -> None:
        from bot import WelcomeTracker

        questions = [
            "а сложно учиться на se?",
            "кто-нибудь поступал на бда?",
            "что посоветуете выбрать: IT или CS?",
            "расскажите про кибербез",
            "подскажите насчет SE",
            "а ты с се?",
            "вы на какую оп поступили?",
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)

            for question in questions:
                tracker = WelcomeTracker(max_messages=10)
                chat_id = 100
                user_id = 123
                tracker.add_user(chat_id, user_id)

                context = MagicMock()
                context.application.bot_data = {
                    "op_registry": registry,
                    "welcome_tracker": tracker,
                }

                update = MagicMock()
                update.effective_message.chat.id = chat_id
                update.effective_message.from_user.id = user_id
                update.effective_message.from_user.is_bot = False
                update.effective_message.reply_to_message = None
                update.effective_message.text = question
                update.effective_message.reply_text = AsyncMock()

                await handle_op_message(update, context)
                update.effective_message.reply_text.assert_not_called()

    async def test_reply_to_other_student_ignored_for_old_member(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=5)
            # Add welcome message for a different newcomer
            tracker.add_welcome_message(100, 500, 111)

            context = MagicMock()
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            # Old member (user 999) replies to student message (msg 700)
            update = MagicMock()
            update.effective_message.chat.id = 100
            update.effective_message.from_user.id = 999
            update.effective_message.from_user.is_bot = False
            update.effective_message.reply_to_message.message_id = 700
            update.effective_message.text = "я сешник"
            update.effective_message.reply_text = AsyncMock()

            await handle_op_message(update, context)
            update.effective_message.reply_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()

