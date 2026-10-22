"""Limits, defaults and UTF-8 helpers. No IO."""
from dataclasses import dataclass
from datetime import datetime, timezone

MAX_IMAGE_BYTES = 5_000_000


@dataclass(frozen=True)
class Config:
    summary_bytes: int = 512
    view_low: int = 64_000
    view_high: int = 128_000
    compact_low: int = 16_000
    compact_high: int = 32_000
    concurrency: int = 8
    attempts: int = 5
    message_bytes: int = 30_000
    page_chars: int = 12_000
    request_bytes: int = 600_000  # one turn's text: file reads and command output add up
    turn_model: str = "claude-sonnet-4-6"
    summary_model: str = "claude-haiku-4-5-20251001"
    agent_model: str = ""  # empty inherits the reply model
    max_tokens: int = 8192
    timeout: float = 120
    max_tool_rounds: int = 64
    instructions: str = ""

    def __post_init__(self):
        for name in ("summary_bytes", "view_low", "compact_low", "concurrency",
                     "attempts", "message_bytes", "page_chars", "request_bytes",
                     "max_tokens", "timeout", "max_tool_rounds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.view_high <= self.view_low or self.compact_high <= self.compact_low:
            raise ValueError("view high watermarks must exceed low watermarks")
        if self.message_bytes < 4:
            raise ValueError("message_bytes must fit a UTF-8 code point")
        if self.concurrency > 8 or self.attempts > 5:
            raise ValueError("at most eight compactions and five attempts are supported")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def byte_size(text: str) -> int:
    return len(text.encode("utf-8"))


def byte_prefix(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def split_text(text: str, limit: int):
    """Lossless UTF-8 chunks of at most `limit` bytes; empty text is one empty chunk."""
    data = text.encode("utf-8")
    if not data:
        yield ""
    offset = 0
    while offset < len(data):
        part = data[offset:offset + limit].decode("utf-8", errors="ignore")
        if not part:
            raise ValueError("chunk limit cannot fit a UTF-8 code point")
        yield part
        offset += len(part.encode("utf-8"))


def clip_output(text: str, limit: int = 30_000) -> str:
    """Keep the head and tail of tool output; the result has at most `limit` characters."""
    marker = "\n[… tool output clipped …]\n"
    if len(text) <= limit:
        return text
    head = (limit - len(marker)) // 2
    return text[:head] + marker + text[-(limit - len(marker) - head):]
