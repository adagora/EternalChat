"""Search parsing, bounded transport and identical tool loops for all connections."""
import asyncio
import io
import json
import tempfile
import os
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import AsyncMock, patch

from eternalchat.app import open_chat
from eternalchat.cli_provider import CLIProvider
from eternalchat.config import Config
from eternalchat.llm import Response
from eternalchat.provider import AnthropicProvider
from eternalchat.search import Search, parse_results
from tests.subscription_fake import FAKE_CLI
from tests.test_provider_cli import sse
from tests.test_engine import Scripted, text_response


def snippet(url="https://example.org/reference", title="Reference &amp; guide", excerpt="An <strong>actual</strong> excerpt."):
    return ('<div class="snippet versioned" data-type="web" data-pos="0">'
            '<div class="result-body"><a href="' + url + '"><div class="site-name">Ignored site name</div>'
            '<div class="title search-snippet-title">' + title + '</div></a>'
            '<div class="generic-snippet"><div>' + excerpt + '</div></div></div></div>')


HTML = '<html><body>' + snippet() + '</body></html>'


class ParserTests(unittest.TestCase):
    def test_order_nested_markup_entities_advertisements_and_duplicates(self):
        html = ('<script>"No results found"</script>'
                + snippet("https://ad.example/", "Advertisement").replace('data-type="web"', 'data-type="ad"')
                + snippet() + snippet()
                + snippet("https://example.net/next", "Café 🦊", "More<br>words."))
        results = parse_results(html)
        self.assertEqual([r["rank"] for r in results], [1, 2])
        self.assertEqual(results[0], {"rank": 1, "title": "Reference & guide", "url": "https://example.org/reference",
                                      "excerpt": "An actual excerpt."})
        self.assertEqual(results[1]["title"], "Café 🦊")
        self.assertEqual(results[1]["excerpt"], "More words.")
        self.assertNotIn("Ignored", str(results))
        self.assertEqual(len(parse_results(html, 1)), 1)

    def test_title_can_wrap_its_link_and_missing_excerpt_stays_empty(self):
        html = ('<div class="snippet" data-type="web"><h3 class="snippet-title">'
                '<a href="https://example.org/">A <em>nested</em> title</a></h3></div>')
        self.assertEqual(parse_results(html)[0]["title"], "A nested title")
        self.assertEqual(parse_results(html)[0]["excerpt"], "")

    def test_no_results_is_distinct_from_challenge_changed_or_truncated_markup(self):
        self.assertEqual(parse_results('<main>No results found for this query.</main>'), [])
        for html in ('<form id="captcha">No results found</form>', '<h1>Verify you are human</h1>',
                     '<main>An unrelated error page</main>', HTML[:-25],
                     '<script>No results found</script><p>Network failure</p>'):
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_results(html)
        self.assertTrue(parse_results(snippet(title="Verify you are human")))
        with self.assertRaises(ValueError):
            parse_results(snippet("javascript:bad", title="No results found"))

    def test_unsafe_links_are_not_sources_and_long_fields_are_bounded(self):
        html = ''.join(snippet(u) for u in ('javascript:alert(1)', 'file:///etc/passwd', '/internal',
                                           'https://name:password@example.org/', 'https://example.org:bad/'))
        html += snippet("https://example.org/safe", "t" * 500, "x" * 5000)
        result = parse_results(html)
        self.assertEqual(len(result), 1)
        self.assertLessEqual(len(result[0]["title"]), 200)
        self.assertLessEqual(len(result[0]["excerpt"]), 800)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def curl(self, data=HTML, status=200, content_type="text/html", code=0, sleep=0):
        payload = data.encode() if isinstance(data, str) else data
        (self.root / "page").write_bytes(payload)
        executable = self.root / "curl"
        executable.write_text("#!" + sys.executable + "\n" +
            "import json,os,pathlib,sys,time\n" +
            "root=pathlib.Path(__file__).parent\n" +
            "root.joinpath('pid').write_text(str(os.getpid()))\n" +
            "assert sys.argv[1] == '--disable'\n" +
            "assert 'ANTHROPIC_API_KEY' not in os.environ\n" +
            "assert 'OPENAI_API_KEY' not in os.environ\n" +
            "url=json.loads(sys.stdin.read().split(' = ',1)[1])\n" +
            "root.joinpath('request').write_text(json.dumps({'url':url,'args':sys.argv[1:]}))\n" +
            f"time.sleep({sleep!r})\n" +
            "sys.stdout.buffer.write(root.joinpath('page').read_bytes())\n" +
            f"sys.stderr.write({str(status)!r}+'\\n'+{content_type!r})\n" +
            f"sys.exit({code!r})\n")
        executable.chmod(0o700)
        return Search(executable=str(executable))

    async def test_query_encoding_no_credentials_config_or_query_in_process_arguments(self):
        search = self.curl()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret", "OPENAI_API_KEY": "secret"}):
            result = json.loads(await search.run({"query": "café & eggs + 日本", "count": 1}))
        request = json.loads((self.root / "request").read_text())
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(request["url"]).query)["q"], ["café & eggs + 日本"])
        self.assertNotIn("café", str(request["args"]))
        self.assertNotIn("--location", request["args"])
        self.assertEqual(result["results"][0]["url"], "https://example.org/reference")

    async def test_invalid_arguments_do_not_start_network_requests(self):
        search = Search()
        with patch.object(search, "_fetch", new_callable=AsyncMock) as network:
            for args in (None, {}, {"query": " "}, {"query": "x" * 501}, {"query": "\ud800"},
                         {"query": "q", "count": True}, {"query": "q", "count": 0},
                         {"query": "q", "count": 11}, {"query": "q", "device": "other"}):
                with self.subTest(args=repr(args)), self.assertRaises(ValueError):
                    await search.run(args)
            network.assert_not_called()

    async def test_network_failures_are_sanitized_and_never_fall_back(self):
        for status, code in ((429, 22), (0, 6), (200, 28), (302, 0)):
            search = self.curl("secret page", status=status, code=code)
            with self.subTest(status=status, code=code), self.assertRaises(ValueError) as caught:
                await search.run({"query": "q"})
            self.assertNotIn("secret", str(caught.exception))
        with patch("eternalchat.search.shutil.which", return_value=None), self.assertRaisesRegex(ValueError, "requires curl"):
            await Search().run({"query": "q"})

    async def test_size_content_type_utf8_and_format_failures_are_not_empty_results(self):
        for data, content_type in ((b"x" * 500_000, "text/html"), ("{}", "application/json"),
                                   ("<p>Broken page</p>", "text/html"), (b"\xffsecret", "text/html")):
            search = self.curl(data, content_type=content_type)
            with self.subTest(data=str(data)[:20]), patch("eternalchat.search.MAX_HTML_BYTES", 1024):
                with self.assertRaises(ValueError) as caught:
                    await search.run({"query": "q"})
                self.assertNotIn("secret", str(caught.exception))
        search = self.curl("<main>No results found</main>")
        self.assertEqual(json.loads(await search.run({"query": "q"}))["status"], "no_results")

    async def test_cancel_terminates_the_request_and_reaps_its_process(self):
        search = self.curl(sleep=30)
        task = asyncio.create_task(search.run({"query": "q"}))
        for _ in range(100):
            if (self.root / "pid").exists():
                break
            await asyncio.sleep(.01)
        self.assertTrue((self.root / "pid").exists())
        pid = int((self.root / "pid").read_text())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        search = self.curl()
        self.assertEqual(json.loads(await search.run({"query": "new"}))["status"], "ok")

    async def test_timeout_cleans_up_and_concurrency_is_bounded(self):
        search = self.curl(sleep=30)
        with patch("eternalchat.search.CALL_TIMEOUT", 0), self.assertRaisesRegex(ValueError, "timed out"):
            await search.run({"query": "q"})
        pid = int((self.root / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        release = asyncio.Event()
        active, most = 0, 0

        async def fetch(query):
            nonlocal active, most
            active += 1
            most = max(most, active)
            try:
                await release.wait()
                return HTML
            finally:
                active -= 1

        with patch.object(search, "_fetch", side_effect=fetch):
            tasks = [asyncio.create_task(search.run({"query": str(i)})) for i in range(8)]
            for _ in range(100):
                if active == 4:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(active, 4)
            release.set()
            await asyncio.gather(*tasks)
            self.assertEqual(most, 4)

    async def test_output_limit_keeps_complete_urls(self):
        html = ''.join(snippet("https://example.org/" + str(i) + "/" + "u" * 1800, "t" * 200, "s" * 800) for i in range(10))
        text = await self.curl(html).run({"query": "q", "count": 10})
        self.assertLessEqual(len(text), 24000)
        results = json.loads(text)["results"]
        self.assertTrue(results)
        self.assertTrue(all(r["url"].endswith("u" * 1800) for r in results))


def stream_reply(content):
    events = [{"type": "message_start", "message": {"usage": {"input_tokens": 1}}}]
    for i, block in enumerate(content):
        events += [{"type": "content_block_start", "index": i, "content_block": block},
                   {"type": "content_block_stop", "index": i}]
    events += [{"type": "message_delta", "delta": {"stop_reason": "tool_use" if content[0]["type"] == "tool_use" else "end_turn"}},
               {"type": "message_stop"}]
    return io.BytesIO(sse(events))


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_search_is_a_logged_tool_error(self):
        async def answer(kw):
            if len(kw["messages"]) == 1:
                return Response([{"type": "tool_use", "id": "s", "name": "search", "input": {"query": "q"}}])
            result = kw["messages"][-1]["content"][0]
            self.assertTrue(result["is_error"])
            self.assertIn("HTTP 429", result["content"][0]["text"])
            return text_response("Search is unavailable.")

        with tempfile.TemporaryDirectory() as root, patch("eternalchat.search.Search._fetch", new_callable=AsyncMock,
                                                        side_effect=ValueError("Brave Search HTTP 429")):
            engine = open_chat(root, Scripted(answer))
            try:
                self.assertEqual(await engine.ask("search"), "Search is unavailable.")
                self.assertEqual(engine.messages()[2]["text"], "Tool error: Brave Search HTTP 429")
            finally:
                await engine.close()

    async def test_stop_during_search_never_logs_a_late_result(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch(query):
            started.set()
            await release.wait()
            return HTML

        async def answer(kw):
            return Response([{"type": "tool_use", "id": "s", "name": "search", "input": {"query": "q"}}])

        with tempfile.TemporaryDirectory() as root, patch("eternalchat.search.Search._fetch", side_effect=fetch):
            engine = open_chat(root, Scripted(answer))
            try:
                reply = engine.submit("search")
                await started.wait()
                engine.stop()
                with self.assertRaises(asyncio.CancelledError):
                    await reply
                release.set()
                await asyncio.sleep(0)
                self.assertEqual([m["kind"] for m in engine.messages()], ["user", "tool"])
            finally:
                release.set()
                await engine.close()

    async def test_chat_and_ordinary_agents_search_with_computer_access_off_in_all_modes(self):
        def api(request, timeout):
            body = json.loads(request.data)
            self.assertIn("search", {t["name"] for t in body["tools"]})
            self.assertNotIn("shell", {t["name"] for t in body["tools"]})
            raw = json.dumps(body["messages"])
            if "Compaction:" in raw:
                return stream_reply([{"type": "text", "text": "summary"}])
            if "tool_result" in raw:
                self.assertIn("https://example.org/reference", raw)
                return stream_reply([{"type": "text", "text": "See [Reference & guide](https://example.org/reference). An actual excerpt."}])
            return stream_reply([{"type": "tool_use", "id": "search1", "name": "search", "input": {"query": "reference guide"}}])

        with tempfile.TemporaryDirectory() as root:
            fake = Path(root, "fake-cli")
            fake.write_text(FAKE_CLI)
            fake.chmod(0o700)
            for mode in ("api", "codex", "claudecode"):
                with self.subTest(mode=mode), patch("eternalchat.search.Search._fetch", new_callable=AsyncMock, return_value=HTML), \
                        patch("eternalchat.provider.urlopen", side_effect=api):
                    provider = AnthropicProvider("fake") if mode == "api" else CLIProvider(mode, executable=str(fake))
                    engine = open_chat(Path(root, mode), provider, Config(turn_model="search-test"))
                    try:
                        self.assertIsNone(engine.machine)
                        self.assertIn("https://example.org/reference", await engine.ask("Find a reference guide"))
                        await engine.start_agent("Research", "Find a reference guide")
                        await engine.agents["Research"]
                        await engine.finish()
                        messages = engine.messages()
                        self.assertTrue(any(m["kind"] == "tool" and '"name": "search"' in m["text"] for m in messages))
                        self.assertTrue(any(m["kind"] == "echo" and "An actual excerpt." in m["text"] for m in messages))
                        transcript = engine.agent_log("Research")["records"]
                        self.assertTrue(any(m["kind"] == "echo" and "https://example.org/reference" in m["text"] for m in transcript))
                        self.assertIn("https://example.org/reference", engine.zoom(id=2).text)
                    finally:
                        await engine.close()
