"""Error types shared by every layer, so core modules never import adapters for them."""


class StoreError(RuntimeError):
    """Saved chat state is unusable, or a write was uncertain. Nothing was repaired."""


class ProviderError(RuntimeError):
    """A model call failed or returned something unusable. It was not retried."""


class CompactionError(RuntimeError):
    """Summaries a turn needs are missing because building them failed."""
