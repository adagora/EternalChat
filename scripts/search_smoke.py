"""Opt-in live search check; no model calls or API credentials.

Run from the checkout: python3 scripts/search_smoke.py [query]
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eternalchat.search import Search


async def main():
    started = time.monotonic()
    result = json.loads(await Search().run({"query": " ".join(sys.argv[1:]) or "Python asyncio documentation", "count": 3}))
    assert result["status"] == "ok" and result["results"], "Live query returned no results"
    assert all(r["url"].startswith(("https://", "http://")) and r["title"] and r["excerpt"] for r in result["results"])
    print(json.dumps({"ok": True, "elapsed_seconds": round(time.monotonic() - started, 3), **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
