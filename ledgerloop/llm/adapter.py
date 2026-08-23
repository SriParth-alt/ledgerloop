"""Provider-agnostic LLM adapter.

TODO(day-9): one `complete(prompt, schema) -> str` interface, with Gemini Flash
and Anthropic implementations selected by env var. temperature=0 always.

The adapter is itself a talking point: no vendor lock-in, and swapping providers
is a config change rather than a refactor.
"""

from __future__ import annotations
