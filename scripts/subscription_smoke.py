"""Opt-in live CLI check; uses the selected account's subscription allowance.

Run from the checkout: python3 scripts/subscription_smoke.py codex
"""
import argparse
import asyncio
import json
from pathlib import Path
import re
import struct
import sys
import tempfile
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eternalchat.app import open_chat
from eternalchat.cli_provider import CLIProvider
from eternalchat.config import Config


def color_image():
    """A real PNG fixture whose answer is available only in the image pixels."""
    width, height = 160, 80
    rows = b''.join(b'\x00' + b'\xff\x00\x00' * (width // 2) +
                    b'\x00\x00\xff' * (width // 2) for _ in range(height))

    def chunk(name, data):
        return struct.pack('!I', len(data)) + name + data + struct.pack('!I', zlib.crc32(name + data))

    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0)) + \
        chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b'')


async def check(client, images=False, streaming=False):
    provider = CLIProvider(client)
    config = Config(turn_model="", summary_model="")
    if streaming:
        events = []
        def stream_event(kind, value):
            events.append((kind, value, time.monotonic()))
        response = await provider.complete(model='', system='Answer the supplied user request.', tools=[],
            messages=[{'role': 'user', 'content': [{'type': 'text', 'text':
                'Explain how a library organizes books in about 200 words. Use normal prose.'}]}],
            max_tokens=1024, on_event=stream_event)
        texts = [value for kind, value, _ in events if kind == 'text']
        assert len(texts) > 1, 'CLI did not deliver incremental reply text'
        assert ''.join(texts) == response.text, events
        assert response.usage.get('input_tokens', 0) + response.usage.get('cache_read_input_tokens', 0) > 0, response.usage
        assert response.usage.get('output_tokens', 0) > 0, response.usage
        assert sum(kind == 'usage' for kind, _, _ in events) == 1, events
        print(json.dumps({'cli': client, 'phase': 'streaming_and_usage', 'ok': True,
            'text_chunks': len(texts), 'thinking_events': sum(kind == 'thinking' for kind, _, _ in events),
            'usage': response.usage, 'connection': provider.stats()}), flush=True)
    events = []

    def observe(kind, value):
        events.append((kind, value))
        if kind in ('error', 'compaction_error'):
            print(json.dumps({'cli': client, 'phase': kind, 'detail': value}), flush=True)

    with tempfile.TemporaryDirectory(prefix="eternalchat-subscription-smoke-") as root:
        engine = open_chat(root, provider, config, observe)
        try:
            engine.note('The exact verification code is amber-42. ' +
                        'This is a test note about a subscription-backed persistent memory. ' * 12)
            await engine.finish()
            status = engine.status()
            assert status['failed'] == 0 and status['summarized_messages'] == 1, status
            print(json.dumps({'cli': client, 'phase': 'model_compaction', 'ok': True}), flush=True)
        finally:
            await engine.close()
        if images:
            image = Path(root, 'fixture with spaces.png')
            image.write_bytes(color_image())
            engine = open_chat(root, provider, config, observe)
            try:
                image_id = engine.status()['messages']
                ref = engine.attach(image)
                answer = await engine.submit('Name the color of the left half and then the right half of this image. '
                                             'Reply with just the two colors in that order.', attachments=[ref])
                assert re.search(r'red\b.*\bblue', answer, re.I | re.S), answer
                print(json.dumps({'cli': client, 'phase': 'image_attachment', 'ok': True}), flush=True)
                await engine.finish()
                assert engine.status()['failed'] == 0, engine.status()
            finally:
                await engine.close()
            image.unlink()  # retrieval must use the durable asset, not the input file
            events.clear()
            engine = open_chat(root, provider, config, observe)
            try:
                answer = await engine.ask(f'Call zoom with id {image_id} to view the original image. '
                    'Use the returned image pixels to name the left and right colors in order. '
                    'Reply with just the two colors.')
                assert any(kind == 'tool' and value.get('name') == 'zoom' and
                           value.get('input', {}).get('id') == image_id for kind, value in events), events
                assert re.search(r'red\b.*\bblue', answer, re.I | re.S), answer
                print(json.dumps({'cli': client, 'phase': 'image_zoom_after_restart', 'ok': True}), flush=True)
                await engine.finish()
                assert engine.status()['failed'] == 0, engine.status()
            finally:
                await engine.close()
        events.clear()
        engine = open_chat(root, provider, config, observe)
        try:
            answer = await engine.ask('Call zoom with id 0 to read the original note, then tell me its exact verification code.')
            await engine.finish()
            assert 'amber-42' in str(answer), answer
            assert any(kind == 'tool' and value.get('name') == 'zoom' for kind, value in events), events
            assert engine.status()['failed'] == 0, engine.status()
            print(json.dumps({'cli': client, 'phase': 'restart_and_memory_tool', 'ok': True}), flush=True)
        finally:
            await engine.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('client', choices=('codex', 'claudecode'))
    parser.add_argument('--images', action='store_true', help='also verify native image input and retrieval after restart')
    parser.add_argument('--streaming', action='store_true', help='also verify incremental output and real token usage')
    args = parser.parse_args()
    asyncio.run(check(args.client, args.images, args.streaming))
