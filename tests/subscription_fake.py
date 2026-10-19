"""Real subprocess fixture implementing the three native CLI protocols."""
FAKE_CLI = r'''#!/usr/bin/env python3
import base64, hashlib, json, os, pathlib, sys
assert 'ANTHROPIC_API_KEY' not in os.environ
assert 'OPENAI_API_KEY' not in os.environ
args = sys.argv[1:]
native = []
codex = 'app-server' in args
def output(value):
    print(json.dumps(value), flush=True)
if codex:
    for line in sys.stdin:
        rpc = json.loads(line)
        method = rpc['method']
        if method == 'initialize':
            result = {'userAgent': 'fake'}
        elif method == 'initialized':
            continue
        elif method == 'account/read':
            result = {'account': {'type': 'chatgpt'}}
        elif method == 'account/rateLimits/read':
            result = {'rateLimits': {'primary': {'usedPercent': 25, 'resetsAt': 1800000000}}}
        elif method == 'thread/start':
            assert rpc['params']['ephemeral'] and rpc['params']['sandbox'] == 'read-only'
            model = rpc['params'].get('model', '')
            result = {'thread': {'id': 'thread-1'}}
        elif method == 'turn/start':
            prompt = rpc['params']['input'][0]['text']
            for block in rpc['params']['input'][1:]:
                if block['type'] == 'localImage':
                    path = pathlib.Path(block['path'])
                    native.append((path.suffix, path.read_bytes()))
            output({'id': rpc['id'], 'result': {'turn': {'id': 'turn-1'}}})
            break
        else:
            raise AssertionError(method)
        output({'id': rpc['id'], 'result': result})
elif '--input-format' in args:
    assert args[args.index('--output-format')+1] == 'stream-json'
    assert '--verbose' in args and '--include-partial-messages' in args
    content = json.loads(sys.stdin.read())['message']['content']
    prompt = content[0]['text']
    for block in content[1:]:
        if block['type'] == 'image':
            native.append((block['source']['media_type'], base64.b64decode(block['source']['data'], validate=True)))
else:
    assert args[args.index('--output-format')+1] == 'streaming-messages-json'
    path = pathlib.Path(args[args.index('--prompt-file')+1])
    assert path.suffix == '.json'
    content = json.loads(path.read_text())
    prompt = content[0]['text']
    for block in content[1:]:
        if block['type'] == 'image':
            native.append((block['mimeType'], base64.b64decode(block['data'], validate=True)))
if not codex:
    model = args[args.index('--model')+1] if '--model' in args else ''
assert 'cache_control' not in prompt
request = json.loads(prompt.rsplit('\n', 1)[1])
if model == 'inspect':
    text = json.dumps({'native': [(mime, hashlib.sha256(data).hexdigest()) for mime, data in native],
                      'request': request, 'args': args, 'cwd': str(pathlib.Path.cwd()),
                      'max_output_tokens': os.environ.get('CLAUDE_CODE_MAX_OUTPUT_TOKENS')})
    reply = {'text': text, 'tool_calls': []}
elif not request['tools']:
    reply = {'text': 'summary', 'tool_calls': []}
elif native:
    assert len(native) == 1
    reply = {'text': 'image inspected', 'tool_calls': []}
elif 'tool_result' in prompt:
    reply = {'text': 'Remembered amber-42.', 'tool_calls': []}
else:
    reply = {'text': '', 'tool_calls': [{'name': 'zoom', 'arguments': {'id': 0}}]}
raw = json.dumps(reply, ensure_ascii=True)
if codex:
    def notify(method, **params):
        output({'method': method, 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', **params}})
    notify('item/reasoning/summaryTextDelta', delta='private reasoning summary')
    for offset in range(0, len(raw), 29):
        notify('item/agentMessage/delta', delta=raw[offset:offset+29])
    notify('item/completed', item={'type': 'agentMessage', 'text': raw})
    notify('thread/tokenUsage/updated', tokenUsage={'total': {'inputTokens': 9, 'cachedInputTokens': 2, 'outputTokens': 3}})
    notify('turn/completed', turn={'status': 'completed'})
else:
    output({'type': 'system', 'subtype': 'init'})
    output({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta':
            {'type': 'thinking_delta', 'thinking': 'private reasoning summary'}}})
    for offset in range(0, len(raw), 29):
        output({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta':
                {'type': 'text_delta', 'text': raw[offset:offset+29]}}})
    output({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': raw}], 'stop_reason': 'end_turn'}})
    output({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': raw,
            'usage': {'input_tokens': 7, 'cache_read_input_tokens': 2, 'output_tokens': 3}})
'''
