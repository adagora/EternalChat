"""The model boundary: what the core asks of a provider and what it gets back.

Messages, content blocks and tool schemas use the Anthropic Messages shape, the
one wire format this application speaks. Adapters implement `Provider`.
"""
from dataclasses import dataclass, field
from typing import Callable, Protocol

# on_event(kind, value): "text" and "thinking" deltas, finished "block"s, "usage".
EventSink = Callable[[str, object], None]


@dataclass
class Response:
    content: list
    stop_reason: str = "end_turn"
    usage: dict = field(default_factory=dict)
    continuation: dict | None = None  # ephemeral provider state; never a stored memory

    def message(self):
        return {"role": "assistant", "content": self.content,
                **({"provider_state": self.continuation} if self.continuation is not None else {})}

    @property
    def text(self):
        return "".join(b["text"] for b in self.content if b["type"] == "text")

    @property
    def tool_calls(self):
        return [b for b in self.content if b["type"] == "tool_use"]


class Provider(Protocol):
    async def complete(self, *, model: str, system: str, tools: list, messages: list,
                       max_tokens: int = 8192, on_event: EventSink | None = None,
                       purpose: str = "turn") -> Response:
        """Run one model call. Raise ProviderError rather than return partial output."""
        ...
