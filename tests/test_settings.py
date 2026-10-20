"""Saved settings, setup, and a plain 'eternalchat' that needs no options."""
import contextlib
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from eternalchat import settings
from eternalchat.cli import default_chat_dir, main, parser, setup

ENV = ("ETERNALCHAT_DIR", "ETERNALCHAT_CONNECTION", "ETERNALCHAT_CLI", "ETERNALCHAT_MODEL",
       "ETERNALCHAT_SUMMARY_MODEL")


class SettingsTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = patch.dict(os.environ, {"ETERNALCHAT_HOME": home.name})
        env.start()
        self.addCleanup(env.stop)
        for key in ENV:
            os.environ.pop(key, None)
        self.home = Path(home.name)

    def answer(self, *answers, which="/usr/local/bin/claude", argv=("setup",)):
        replies = iter(answers)
        args = parser(settings.load()).parse_args(list(argv))
        with patch("eternalchat.cli.shutil.which", return_value=which):
            return setup(args, read=lambda prompt: next(replies), write=lambda *a: None)

    def test_option_beats_environment_beats_saved_setting(self):
        saved = {"connection": "subscription", "cli": "claudecode", "model": "sonnet", "data_dir": "/chats/one"}
        args = parser(saved).parse_args(["ask", "hi"])
        self.assertEqual((args.connection, args.cli, args.model, args.data_dir),
                         ("subscription", "claudecode", "sonnet", "/chats/one"))
        os.environ["ETERNALCHAT_MODEL"] = "opus"
        self.assertEqual(parser(saved).parse_args(["ask", "hi"]).model, "opus")
        for argv in (["ask", "hi", "--model", "haiku"], ["--model", "haiku", "ask", "hi"]):
            self.assertEqual(parser(saved).parse_args(argv).model, "haiku")
        del os.environ["ETERNALCHAT_MODEL"]
        args = parser().parse_args(["ask", "hi"])
        self.assertEqual((args.connection, args.cli, args.model, args.data_dir), ("api", "codex", None, None))

    def test_settings_file_is_private_and_keeps_only_set_values(self):
        target = settings.save({"connection": "api", "cli": None, "model": "", "data_dir": Path("/x"),
                                "unknown": "ignored"})
        self.assertEqual(settings.load(), {"connection": "api", "data_dir": "/x"})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_broken_settings_file_is_reported_and_setup_replaces_it(self):
        settings.path().parent.mkdir(parents=True, exist_ok=True)
        settings.path().write_text('{"model": 5}')
        with self.assertRaisesRegex(ValueError, "eternalchat setup"):
            settings.load()
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main(["status", "--data-dir", str(self.home / "none")]), 1)
        self.assertIn("not a valid settings file", err.getvalue())
        replies = iter(["1", "", "", ""])
        with patch("builtins.input", lambda prompt: next(replies)), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["setup"]), 0)
        self.assertEqual(settings.load()["connection"], "api")

    def test_setup_saves_answers_and_rerunning_keeps_them(self):
        # Claude Code; Enter keeps the CLI's default model, the suggested summary
        # model and the default chat folder.
        self.assertTrue(self.answer("2", "", "", ""))
        chat = str(settings.default_chat().resolve())
        expected = {"connection": "subscription", "cli": "claudecode", "summary_model": "haiku", "data_dir": chat}
        self.assertEqual(settings.load(), expected)
        self.assertTrue(self.answer("", "", "", ""))
        self.assertEqual(settings.load(), expected)
        self.assertTrue(self.answer("", "sonnet", "default", ""))
        self.assertEqual(settings.load(), {**{k: v for k, v in expected.items() if k != "summary_model"},
                                           "model": "sonnet"})

    def test_switching_connection_starts_models_over(self):
        self.assertTrue(self.answer("2", "opus", "haiku", "", argv=("setup", "--reasoning-effort", "high")))
        self.assertEqual(settings.load()["reasoning_effort"], "high")
        self.assertTrue(self.answer("1", "", "", ""))
        self.assertEqual(settings.load(), {"connection": "api", "data_dir": str(settings.default_chat().resolve())})

    def test_cancelled_setup_saves_nothing(self):
        def cancel(prompt):
            raise EOFError
        args = parser().parse_args(["setup"])
        self.assertFalse(setup(args, read=cancel, write=lambda *a: None))
        self.assertFalse(settings.path().exists())

    def test_chat_in_the_working_folder_is_kept(self):
        with tempfile.TemporaryDirectory() as work, contextlib.chdir(work):
            self.assertEqual(default_chat_dir(), settings.default_chat())
            Path(".eternalchat", "main").mkdir(parents=True)
            self.assertEqual(default_chat_dir(), settings.LEGACY_CHAT)

    def test_plain_command_chats_and_its_first_run_sets_up(self):
        def first_setup(args):
            settings.save({"connection": "subscription", "cli": "claudecode"})
            return True

        with patch("eternalchat.cli.run", new_callable=AsyncMock, return_value=0) as run, \
                patch("eternalchat.cli.setup", side_effect=first_setup) as asked, \
                patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True):
            self.assertEqual(main([]), 0)
            self.assertEqual(asked.call_count, 1)
            args = run.call_args.args[0]
            self.assertEqual((args.command, args.connection, args.cli), ("chat", "subscription", "claudecode"))
            self.assertEqual(main([]), 0)  # settings exist now: straight to the chat
            self.assertEqual(asked.call_count, 1)
            self.assertEqual(main(["setup"]), 0)
            self.assertEqual((asked.call_count, run.call_count), (2, 2))
            settings.path().unlink()
            self.assertEqual(main(["web", "--no-browser"]), 0)  # a first 'web' sets up too
            self.assertEqual((asked.call_count, run.call_args.args[0].command), (3, "web"))
