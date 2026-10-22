"""Real subprocess fixture implementing the two native CLI protocols."""
FAKE_CLI = r'''#!/usr/bin/env python3
import base64, hashlib, json, os, pathlib, sys
assert 'ANTHROPIC_API_KEY' not in os.environ
assert 'OPENAI_API_KEY' not in os.environ
args = sys.argv[1:]
native = []
marks = []
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
            result = {'thread': {'id': 'thread-1'}, 'model': model or 'fixture-codex', 'reasoningEffort': 'medium'}
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
else:
    assert args[args.index('--output-format')+1] == 'stream-json'
    assert '--verbose' in args and '--include-partial-messages' in args
    content = json.loads(sys.stdin.read())['message']['content']
    # The prompt arrives as text blocks, then numbered image references.
    refs = next((k for k, b in enumerate(content) if b['type'] != 'text'
                 or b['text'].startswith('[EternalChat image ')), len(content))
    prompt = ''.join(b['text'] for b in content[:refs])
    marks = [b['cache_control'] for b in content if 'cache_control' in b]
    assert marks in ([], [{'type': 'ephemeral', 'ttl': '1h'}]), marks
    for block in content[refs:]:
        if block['type'] == 'image':
            native.append((block['source']['media_type'], base64.b64decode(block['source']['data'], validate=True)))
if not codex:
    model = args[args.index('--model')+1] if '--model' in args else ''
assert 'cache_control' not in prompt
request = json.loads(prompt.rsplit('\n', 1)[1])
if model == 'inspect':
    text = json.dumps({'native': [(mime, hashlib.sha256(data).hexdigest()) for mime, data in native],
                      'request': request, 'args': args, 'cwd': str(pathlib.Path.cwd()), 'marks': marks,
                      'max_output_tokens': os.environ.get('CLAUDE_CODE_MAX_OUTPUT_TOKENS')})
    reply = {'text': text, 'tool_calls': []}
elif not request['tools']:
    reply = {'text': 'summary', 'tool_calls': []}
elif model == 'search-test':
    assert 'search' in {t['name'] for t in request['tools']}
    assert 'shell' not in {t['name'] for t in request['tools']}
    if 'Compaction:' in json.dumps(request['messages']):
        reply = {'text': 'summary', 'tool_calls': []}
    elif 'tool_result' in prompt:
        assert 'https://example.org/reference' in prompt
        assert 'An actual excerpt.' in prompt
        reply = {'text': 'See [Reference & guide](https://example.org/reference). An actual excerpt.', 'tool_calls': []}
    else:
        reply = {'text': '', 'tool_calls': [{'name': 'search', 'arguments': {'query': 'reference guide'}}]}
elif model == 'screen-test':
    if 'Compaction:' in json.dumps(request['messages']):
        reply = {'text': 'summary', 'tool_calls': []}
    elif {t['name'] for t in request['tools']} == {'computer'}:
        assert 'action' in request['tools'][0]['input_schema']['properties']
        assert 'device' not in request['tools'][0]['input_schema']['properties']
        if native:
            assert len(native) == 1 and native[0][1].startswith(b'\xff\xd8\xff')
            reply = {'text': 'Screen inspected through native image input.', 'tool_calls': []}
        else:
            reply = {'text': '', 'tool_calls': [{'name': 'computer', 'arguments': {'action': 'screenshot'}}]}
    elif any(b.get('text') == 'Start a screen task' for m in request['messages'] for b in m.get('content', [])) and 'tool_result' not in prompt:
        reply = {'text': '', 'tool_calls': [{'name': 'computer', 'arguments': {'task': 'Inspect the screen'}}]}
    else:
        reply = {'text': 'Worker is in the background.', 'tool_calls': []}
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
    output({'type': 'system', 'subtype': 'init', 'model': model or 'fixture-claude'})
    output({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta':
            {'type': 'thinking_delta', 'thinking': 'private reasoning summary'}}})
    for offset in range(0, len(raw), 29):
        output({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta':
                {'type': 'text_delta', 'text': raw[offset:offset+29]}}})
    output({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': raw}], 'stop_reason': 'end_turn'}})
    output({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': raw,
            'usage': {'input_tokens': 7, 'cache_read_input_tokens': 2, 'output_tokens': 3}})
'''
