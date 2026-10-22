"""Public Brave Search HTML, shared by API and subscription conversations.

No model generates these results and no search API key is used. Only organic
web snippets are extracted, in page order; challenges and changed markup fail
explicitly. This public page is not a guaranteed API.
"""
import asyncio
import json
import os
import shutil
import urllib.parse
from html.parser import HTMLParser

ENDPOINT = "https://search.brave.com/search"
MAX_HTML_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 24_000
MAX_RESULTS = 10
SOCKET_TIMEOUT = 10
CALL_TIMEOUT = 20
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def _plain(text, limit):
    return " ".join(text.split())[:limit]


def _url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        parsed.port  # validate malformed ports too
    except ValueError:
        return None
    return value


class _Results(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.items, self.visible = [], [], []
        self.item = None
        self.blocked = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        frame = {"tag": tag, "classes": classes, "href": attrs.get("href")}
        if tag not in VOID:
            self.stack.append(frame)
        elif tag == "br":
            self.handle_data(" ")
        marker = (attrs.get("id", "") + " " + attrs.get("class", "")).lower()
        if "captcha" in marker or "challenge-form" in marker:
            self.blocked = True
        if "snippet" in classes and attrs.get("data-type") == "web" and self.item is None:
            self.item = {"depth": len(self.stack), "title": [], "excerpt": [], "url": None}
        if self.item and classes & {"search-snippet-title", "snippet-title"}:
            self.item["url"] = next((f["href"] for f in reversed(self.stack) if f["tag"] == "a"), None)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, -1, -1)
                      if self.stack[i]["tag"] == tag), None)
        if index is None:
            return
        if self.item and index < self.item["depth"]:
            self.items.append(self.item)
            self.item = None
        del self.stack[index:]

    def handle_data(self, data):
        if any(f["tag"] in {"script", "style", "svg"} for f in self.stack):
            return
        self.visible.append(data)
        if self.item:
            classes = set().union(*(f["classes"] for f in self.stack[self.item["depth"] - 1:]))
            if classes & {"search-snippet-title", "snippet-title"}:
                self.item["title"].append(data)
                if self.item["url"] is None:
                    self.item["url"] = next((f["href"] for f in reversed(self.stack) if f["tag"] == "a"), None)
            elif "generic-snippet" in classes:
                self.item["excerpt"].append(data)


def parse_results(html, count=5):
    """Validate real result markup; never mistake an error page for no matches."""
    page = _Results()
    page.feed(html)
    page.close()
    visible = _plain(" ".join(page.visible), MAX_HTML_BYTES).lower()
    if page.blocked or (not page.items and any(s in visible for s in (
            "verify you are human", "unusual traffic", "checking your browser"))):
        raise ValueError("Brave Search requires a browser verification; search is unavailable from this server")
    if page.item is not None:
        raise ValueError("Brave Search returned an incomplete results page")
    results, seen = [], set()
    for item in page.items:
        url = _url(item["url"])
        title = _plain("".join(item["title"]), 200)
        if not url or not title or url in seen:
            continue
        seen.add(url)
        results.append({"rank": len(results) + 1, "title": title, "url": url,
                        "excerpt": _plain("".join(item["excerpt"]), 800)})
        if len(results) == count:
            break
    if not results and (page.items or not any(s in visible for s in (
            "no results found", "no results were found", "couldn't find any results"))):
        raise ValueError("Brave Search returned an unrecognized page; no results could be verified")
    return results


class Search:
    def __init__(self, executable=None):
        self.executable = executable
        self._slots = asyncio.Semaphore(4)

    async def run(self, args):
        if not isinstance(args, dict) or set(args) - {"query", "count"}:
            raise ValueError("search requires query and optional count")
        query, count = args.get("query"), args.get("count", 5)
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError("search query must contain 1–500 characters")
        if type(count) is not int or not 1 <= count <= MAX_RESULTS:
            raise ValueError("search count must be an integer from 1 to 10")
        query = " ".join(query.split())
        try:
            query.encode("utf-8")
        except UnicodeError:
            raise ValueError("search query must be valid Unicode") from None
        async with self._slots:
            try:
                html = await asyncio.wait_for(self._fetch(query), CALL_TIMEOUT + 1)
            except TimeoutError:
                raise ValueError("Brave Search timed out; no alternate backend was used") from None
        results = parse_results(html, count)
        value = {"backend": "Brave Search (public web page)", "query": query,
                 "status": "ok" if results else "no_results", "results": results}
        while len(json.dumps(value, ensure_ascii=False)) > MAX_OUTPUT_CHARS:
            value["results"].pop()
        return json.dumps(value, ensure_ascii=False)

    async def _fetch(self, query):
        executable = self.executable or shutil.which("curl")
        if not executable:
            raise ValueError("Web search requires curl; install curl on the EternalChat server")
        url = ENDPOINT + "?" + urllib.parse.urlencode({"q": query, "source": "web"})
        # --disable must be first: ignore .curlrc (custom headers, cookies, etc.).
        # Pass the query on stdin, not in the process list; never follow redirects.
        command = [executable, "--disable", "--silent", "--fail", "--proto", "=https",
                   "--max-time", str(CALL_TIMEOUT), "--connect-timeout", str(SOCKET_TIMEOUT),
                   "--max-filesize", str(MAX_HTML_BYTES), "--write-out", "%{stderr}%{response_code}\n%{content_type}",
                   "--config", "-"]
        env = {k: v for k, v in os.environ.items() if not k.startswith("ETERNALCHAT_") and k not in {
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}}
        try:
            process = await asyncio.create_subprocess_exec(*command, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError:
            raise ValueError("Web search could not start curl; check the server installation") from None

        async def bounded(stream, limit):
            data = bytearray()
            while chunk := await stream.read(16_384):
                data += chunk
                if len(data) > limit:
                    raise ValueError("Brave Search response exceeds its size limit")
            return bytes(data)

        readers = []
        try:
            process.stdin.write(("url = " + json.dumps(url) + "\n").encode())
            await process.stdin.drain()
            process.stdin.close()
            readers = [asyncio.create_task(bounded(process.stdout, MAX_HTML_BYTES)),
                       asyncio.create_task(bounded(process.stderr, 4096))]
            data, meta = await asyncio.gather(*readers)
            code = await process.wait()
            status, _, content_type = meta.partition(b"\n")
            if status.isdigit() and int(status) >= 400:
                raise ValueError(f"Brave Search HTTP {int(status)}; no alternate backend was used")
            if code or status != b"200":
                raise ValueError(f"Brave Search transport failed (curl exit {code}); no alternate backend was used")
            if content_type.split(b";", 1)[0].strip().lower() != b"text/html":
                raise ValueError("Brave Search returned an unsupported content type")
            try:
                return data.decode("utf-8")
            except UnicodeError:
                raise ValueError("Brave Search returned invalid UTF-8") from None
        except (BrokenPipeError, ConnectionResetError):
            raise ValueError("Brave Search transport stopped; no alternate backend was used") from None
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            # Drain after killing, including on an oversize response. A paused
            # stdout transport must not keep process.wait() waiting forever.
            async def discard(stream):
                while await stream.read(16_384):
                    pass
            await asyncio.gather(discard(process.stdout), discard(process.stderr))
            await process.wait()
