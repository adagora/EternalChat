"""Offline provider for demos and tests: deterministic placeholders, deliberately not an LLM."""
import asyncio

from .config import byte_prefix
from .llm import Response


class DemoProvider:
    async def complete(self, *, model, system, tools, messages, max_tokens=8192, on_event=None, purpose="turn"):
        await asyncio.sleep(0)
        blocks = messages[-1]["content"]
        text = next((b["text"] for b in reversed(blocks) if b["type"] == "text"), "")
        if text.startswith(("Compaction:", "Too long:")):
            source = text.split("<input>\n", 1)[-1].split("\n</input>", 1)[0]
            result = byte_prefix(source.replace("\n", " "), 480)
        elif any(b["type"] == "tool_result" for b in blocks):
            result = "[Offline demo] Retrieved: " + " ".join(
                c.get("text", "") for b in blocks if b["type"] == "tool_result" for c in b["content"])
        elif text.strip().startswith("/recall "):
            try:
                i = int(text.strip().split()[-1])
            except ValueError:
                i = -1
            block = {"type": "tool_use", "id": "demo_zoom", "name": "zoom", "input": {"id": i, "n": 1}}
            if on_event:
                on_event("block", block)
            return Response([block], "tool_use")
        else:
            result = "[Offline demo] Saved your message: " + text.split("\n\n", 1)[-1]
        block = {"type": "text", "text": result}
        if on_event:
            on_event("text", result)
            on_event("block", block)
        return Response([block])
