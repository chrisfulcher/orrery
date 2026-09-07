"""The Anthropic backend, through the official SDK.

Structured output comes from the Messages API's JSON-schema output format; thinking stays at
the model's adaptive default; nothing is sampled by temperature. On Claude Opus 5 and Fable
models the request opts into server-side refusal fallbacks so a policy decline is retried
on a fallback model inside the same call. The key is passed only when the slot has one;
otherwise the SDK resolves its own (``ANTHROPIC_API_KEY`` or an ``ant auth`` profile).
"""

from typing import Any

import anthropic

from mentor import __version__
from mentor.ai import AIError, Completion, Slot, redact

FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-")


class AnthropicBackend:
    def __init__(self, slot: Slot, client: Any | None = None) -> None:
        self.slot = slot
        if client is None:
            options: dict[str, Any] = {
                "timeout": slot.timeout,
                "max_retries": 2,
                "default_headers": {"User-Agent": f"mentor/{__version__}"},
            }
            if slot.api_key is not None:
                options["api_key"] = slot.api_key.get_secret_value()
            if slot.base_url:
                options["base_url"] = slot.base_url
            client = anthropic.Anthropic(**options)
        self._client = client

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()

    def complete(self, *, system: str, user: str, schema: dict, max_tokens: int) -> Completion:
        request: dict[str, Any] = {
            "model": self.slot.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        try:
            if self.slot.model.startswith(FALLBACK_MODELS):
                response = self._client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **request
                )
            else:
                response = self._client.messages.create(**request)
        except anthropic.AuthenticationError as exc:
            raise AIError(self._redact(f"anthropic: authentication failed: {exc}")) from exc
        except anthropic.RateLimitError as exc:
            raise AIError(self._redact(f"anthropic: rate limited: {exc}")) from exc
        except anthropic.APIStatusError as exc:
            raise AIError(self._redact(f"anthropic: HTTP {exc.status_code}: {exc}")) from exc
        except anthropic.APIConnectionError as exc:
            raise AIError(self._redact(f"anthropic: {type(exc).__name__}: {exc}")) from exc
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise AIError(
                f"{self.slot.model} declined the request" + (f" ({category})" if category else "")
            )
        if stop == "max_tokens":
            raise AIError(f"{self.slot.model} ran out of output tokens ({max_tokens})")
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
        usage = getattr(response, "usage", None)
        return Completion(
            text,
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
            stop,
        )

    def _redact(self, text: str) -> str:
        return redact(text, [self.slot.api_key])
