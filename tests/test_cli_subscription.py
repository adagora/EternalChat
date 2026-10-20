import asyncio
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from eternalchat.cli import UsageError, parser, run
from eternalchat.cli_provider import CLIProvider, decode_reply
from eternalchat.errors import ProviderError
from eternalchat.prompt import TOOLS, image_block


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5WQAAAAASUVORK5CYII=')
from tests.subscription_fake import FAKE_CLI as VISION_CLI


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_clients_use_real_process_and_memory_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            fake = Path(root) / "fake-cli"
            fake.write_text(VISION_CLI)
            fake.chmod(0o700)
            from eternalchat.app import open_chat
            for client in ('codex', 'claudecode'):
                with self.subTest(client=client), patch.dict(os.environ, {
                        'ANTHROPIC_API_KEY': 'secret', 'OPENAI_API_KEY': 'secret'}):
                    provider = CLIProvider(client, executable=str(fake))
                    engine = open_chat(Path(root) / client, provider)
                    try:
                        engine.note('amber-42')
                        response = await engine.ask('Recall my note')
                        await engine.finish()
                        self.assertIn('amber-42', str(response))
                        self.assertIn('amber-42', engine.zoom(id=0).text)
                    finally:
                        await engine.close()

    async def test_summary_and_events(self):
        with tempfile.TemporaryDirectory() as root:
            fake = Path(root) / 'fake'
            fake.write_text(VISION_CLI)
            fake.chmod(0o700)
            events = []
            response = await CLIProvider('claudecode', executable=str(fake)).complete(
                model='', system='Compaction', tools=[], messages=[],
                on_event=lambda *event: events.append(event))
            self.assertEqual(response.text, 'summary')
            self.assertEqual(''.join(v for kind, v in events if kind == 'text'), 'summary')
            self.assertEqual(sum(kind == 'block' for kind, _ in events), 1)
            self.assertEqual(response.usage['input_tokens'], 7)

    async def test_timeout_and_cancellation_reap_process(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as root:
                pid_file = Path(root) / 'pid'
                cwd_file = Path(root) / 'cwd'
                fake = Path(root) / 'fake'
                fake.write_text('#!/usr/bin/env python3\nimport os, time, pathlib\n'
                                f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n'
                                f'pathlib.Path({str(cwd_file)!r}).write_text(str(pathlib.Path.cwd()))\n'
                                'time.sleep(60)\n')
                fake.chmod(0o700)
                provider = CLIProvider('claudecode', executable=str(fake), timeout=2 if not cancel else 30)
                task = asyncio.create_task(provider.complete(model='', system='', tools=[],
                    messages=[{'role': 'user', 'content': [image_block('image/png', PNG)]}]))
                for _ in range(400):
                    if pid_file.exists():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(pid_file.exists())
                if cancel:
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else ProviderError):
                    await task
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), 0)
                self.assertFalse(Path(cwd_file.read_text()).exists())

    async def test_exit_error_does_not_echo_output(self):
        with tempfile.TemporaryDirectory() as root:
            fake = Path(root) / 'fake'
            fake.write_text('#!/bin/sh\necho secret\necho secret >&2\nexit 7\n')
            fake.chmod(0o700)
            with self.assertRaisesRegex(ProviderError, 'malformed JSON') as caught:
                await CLIProvider('claudecode', executable=str(fake)).complete(
                    model='', system='', tools=[], messages=[])
            self.assertNotIn('secret', str(caught.exception))

    def test_strict_protocol(self):
        for value in ('garbage', '{}', '[]', '{"text":"", "tool_calls":[]}',
                      '{"text":"hi", "tool_calls":[{"name":"shell", "arguments":{}}]}',
                      '{"text":"hi", "tool_calls":[{"name":[], "arguments":{}}]}'):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                decode_reply(value, TOOLS)
        with self.assertRaises(ProviderError):
            decode_reply('{"text":"", "tool_calls":[{"name":"zoom", "arguments":{}}]}', [])

    def test_options_work_on_both_sides(self):
        for argv in (["--connection", "subscription", "--cli", "claudecode", "ask", "hi"],
                     ["ask", "hi", "--connection", "subscription", "--cli", "claudecode"]):
            args = parser().parse_args(argv)
            self.assertEqual((args.connection, args.cli, args.model), ('subscription', 'claudecode', None))

    async def test_grok_is_no_longer_a_client(self):
        with patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            parser().parse_args(['--connection', 'subscription', '--cli', 'grok', 'ask', 'hi'])
        with patch.dict(os.environ, {'ETERNALCHAT_CLI': 'grok'}):
            args = parser().parse_args(['--connection', 'subscription', 'ask', 'hi'])
        with self.assertRaisesRegex(UsageError, 'Grok support was removed'):
            await run(args)
        with self.assertRaises(ValueError):
            CLIProvider('grok')

    async def test_streaming_options_are_wired_to_the_provider(self):
        engine = MagicMock()
        engine.close = AsyncMock()
        with patch('eternalchat.cli.open_chat', return_value=engine) as opened, \
             patch('eternalchat.cli.CLIProvider') as provider, \
             patch.dict('eternalchat.cli.COMMANDS', {'ask': AsyncMock(return_value=0)}):
            await run(parser().parse_args(['ask', 'hi', '--connection', 'subscription', '--cli', 'claudecode',
                '--cli-concurrency', '2', '--reasoning-effort', 'medium', '--max-tokens', '1024']))
            self.assertEqual(provider.call_args.kwargs['concurrency'], 2)
            self.assertEqual(provider.call_args.kwargs['reasoning_effort'], 'medium')
            self.assertEqual(opened.call_args.args[2].max_tokens, 1024)

    async def test_native_output_budget_overrides_inherited_claude_setting(self):
        with tempfile.TemporaryDirectory() as root:
            fake = Path(root, 'fake')
            fake.write_text(VISION_CLI)
            fake.chmod(0o700)
            with patch.dict(os.environ, {'CLAUDE_CODE_MAX_OUTPUT_TOKENS': '9999'}):
                for client in ('codex', 'claudecode'):
                    with self.subTest(client=client):
                        response = await CLIProvider(client, executable=str(fake)).complete(
                            model='inspect', system='', tools=[], messages=[], max_tokens=4096)
                        evidence = json.loads(response.text)
                        if client == 'claudecode':
                            self.assertEqual(evidence['max_output_tokens'], '4096')
                            self.assertNotIn('response_token_budget', evidence['request'])
                        else:
                            self.assertEqual(evidence['request']['response_token_budget'], 4096)
                self.assertEqual(os.environ['CLAUDE_CODE_MAX_OUTPUT_TOKENS'], '9999')

    async def test_readonly_needs_no_installed_cli(self):
        with tempfile.TemporaryDirectory() as root:
            await run(parser().parse_args(['--demo', '--data-dir', root, 'note', 'hello']))
            with patch('eternalchat.cli.CLIProvider', side_effect=AssertionError):
                await run(parser().parse_args(['--data-dir', root, '--connection', 'subscription',
                                              '--cli-executable', '/missing', 'status']))

    async def test_connection_model_defaults_and_overrides(self):
        cases = [([], 'claude-sonnet-4-6', 'claude-haiku-4-5-20251001'),
                 (['--connection', 'subscription'], '', ''),
                 (['--connection', 'subscription', '--model', 'custom'], 'custom', 'custom'),
                 (['--connection', 'subscription', '--model', 'custom',
                   '--summary-model', 'small'], 'custom', 'small')]
        for flags, turn, summary in cases:
            with self.subTest(flags=flags), patch.dict(os.environ, {}, clear=True):
                engine = MagicMock()
                engine.close = AsyncMock()
                with patch('eternalchat.cli.open_chat', return_value=engine) as opened, \
                     patch('eternalchat.cli.AnthropicProvider'), \
                     patch('eternalchat.cli.CLIProvider'), \
                     patch.dict('eternalchat.cli.COMMANDS', {'ask': AsyncMock(return_value=0)}):
                    await run(parser().parse_args([*flags, 'ask', 'hello']))
                    config = opened.call_args.args[2]
                    self.assertEqual((config.turn_model, config.summary_model), (turn, summary))

    async def test_native_images_preserve_order_history_and_nested_tool_results(self):
        samples = [('image/png', PNG + b'x' * 4_900_000), ('image/jpeg', b'\xff\xd8\xffphoto'),
                   ('image/gif', b'GIF89aimage'), ('image/webp', b'RIFF0000WEBPimage')]
        blocks = [image_block(mime, data) for mime, data in samples]
        messages = [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'Compare these', 'cache_control': {'type': 'ephemeral'}}, *blocks[:2]]},
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'old-call',
                'name': 'zoom', 'input': {'id': 0}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'old-call',
                'content': [{'type': 'text', 'text': 'original message'}, *blocks[2:]], 'is_error': False}]}]
        original = copy.deepcopy(messages)
        with tempfile.TemporaryDirectory() as root:
            fake = Path(root, 'vision-cli')
            fake.write_text(VISION_CLI)
            fake.chmod(0o700)
            for client in ('codex', 'claudecode'):
                with self.subTest(client=client):
                    response = await CLIProvider(client, executable=str(fake)).complete(
                        model='inspect', system='test system', tools=TOOLS, messages=messages)
                    evidence = json.loads(response.text)
                    expected_types = ['.png', '.jpg', '.gif', '.webp'] if client == 'codex' else [m for m, _ in samples]
                    self.assertEqual(evidence['native'], [[mime, hashlib.sha256(data).hexdigest()]
                        for mime, (_, data) in zip(expected_types, samples)])
                    sent = evidence['request']['messages']
                    self.assertEqual(sent[0]['content'][1]['text'], '[EternalChat image 1]')
                    self.assertEqual(sent[0]['content'][2]['text'], '[EternalChat image 2]')
                    result = sent[2]['content'][0]
                    self.assertEqual(result['tool_use_id'], 'old-call')
                    self.assertFalse(result['is_error'])
                    self.assertEqual(result['content'][1]['text'], '[EternalChat image 3]')
                    self.assertEqual(result['content'][2]['text'], '[EternalChat image 4]')
                    self.assertEqual(sent[1], messages[1])
                    self.assertLess(len(json.dumps(evidence['args'])), 4000)
                    self.assertFalse(Path(evidence['cwd']).exists())
                    self.assertEqual(evidence['marks'], [{'type': 'ephemeral', 'ttl': '1h'}]
                                     if client == 'claudecode' else [])
        self.assertEqual(messages, original)

    async def test_image_cli_input_and_retrieval_after_restart(self):
        from eternalchat.app import open_chat
        from eternalchat.cli import Terminal, Session
        from eternalchat.config import Config
        with tempfile.TemporaryDirectory() as root:
            fake = Path(root, 'vision-cli')
            fake.write_text(VISION_CLI)
            fake.chmod(0o700)
            image = Path(root, 'picture with spaces.png')
            image.write_bytes(PNG)
            for client in ('codex', 'claudecode'):
                data = Path(root, client)
                with self.subTest(client=client), patch('sys.stdout', new_callable=io.StringIO):
                    # The public ask path persists and forwards --image.
                    code = await run(parser().parse_args(['--data-dir', str(data), '--connection',
                        'subscription', '--cli', client, '--cli-executable', str(fake),
                        'ask', 'Look at this', '--image', str(image)]))
                    self.assertEqual(code, 0)
                    events = []
                    engine = open_chat(data, CLIProvider(client, executable=str(fake)),
                        Config(turn_model='', summary_model=''), lambda *event: events.append(event))
                    try:
                        answer = await engine.ask('Retrieve the stored image')
                        self.assertEqual(answer, 'image inspected')
                        self.assertTrue(any(kind == 'tool' and value['name'] == 'zoom'
                                            for kind, value in events))
                        # The interactive /attach path feeds the same provider.
                        session = Session(engine, Terminal())
                        session.handle(f'/attach "{image}"')
                        session.handle('Look at the new image')
                        await asyncio.gather(*session.futures)
                        await engine.finish()
                        self.assertEqual(engine.status()['failed'], 0)
                    finally:
                        await engine.close()

    async def test_invalid_images_fail_before_spawning(self):
        bad = [{'type': 'image'}, image_block('image/svg+xml', PNG),
               {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.org/image.png'}},
               {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'secret!'}},
               image_block('image/png', b''), image_block('image/png', b'x' * 5_000_001)]
        for client in ('codex', 'claudecode'):
            with patch('eternalchat.cli_provider.shutil.which', return_value='/fake'):
                provider = CLIProvider(client)
            for block in bad:
                with self.subTest(client=client, source=block.get('source', {}).get('type')), \
                     patch('asyncio.create_subprocess_exec') as spawn, self.assertRaises(ProviderError) as caught:
                    await provider.complete(model='', system='', tools=[],
                        messages=[{'role': 'user', 'content': [block]}])
                spawn.assert_not_called()
                self.assertNotIn('secret!', str(caught.exception))

    async def test_claude_image_result_errors_do_not_become_answers(self):
        with tempfile.TemporaryDirectory() as root:
            fake = Path(root, 'fake')
            for result in ({'type': 'result', 'subtype': 'error', 'is_error': True, 'result': 'secret'},
                           {'type': 'assistant', 'result': 'secret'}, {}, 'secret'):
                fake.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n' +
                                f'print({json.dumps(result)!r})\n')
                fake.chmod(0o700)
                with self.assertRaises(ProviderError) as caught:
                    await CLIProvider('claudecode', executable=str(fake)).complete(
                        model='', system='', tools=[], messages=[{'role': 'user',
                            'content': [image_block('image/png', PNG)]}])
                self.assertNotIn('secret', str(caught.exception))
