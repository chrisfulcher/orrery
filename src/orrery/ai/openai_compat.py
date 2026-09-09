"""An OpenAI-compatible chat backend: Ollama, LM Studio, llama.cpp, vLLM, OpenAI, OpenRouter.

Talks to the slot's base URL and nothing else. The key, when set, is sent as a Bearer token
and never appears in an exception message. Structured output is requested through
``response_format`` with a JSON schema, which Ollama enforces by grammar and most services
honor; the caller validates regardless.
"""

import httpx

from orrery import __version__
from orrery.ai import AIError, Completion, Slot, redact


class OpenAIChatBackend:
    def __init__(self, slot: Slot, http: httpx.Client | None = None) -> None:
        if not slot.base_url:
            raise AIError(f"the {slot.name} slot has no base URL")
        self.slot = slot
        self._url = slot.base_url.rstrip("/") + "/chat/completions"
        headers = {"User-Agent": f"orrery/{__version__}"}
        if slot.api_key is not None:
            headers["Authorization"] = f"Bearer {slot.api_key.get_secret_value()}"
        self._http = http or httpx.Client(timeout=slot.timeout, headers=headers)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OpenAIChatBackend":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def complete(self, *, system: str, user: str, schema: dict, max_tokens: int) -> Completion:
        body = {
            "model": self.slot.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": schema, "strict": True},
            },
        }
        try:
            response = self._http.post(self._url, json=body)
        except httpx.HTTPError as exc:
            raise AIError(self._redact(f"{self._url}: {type(exc).__name__}: {exc}")) from exc
        if not response.is_success:
            raise AIError(f"{self._url}: HTTP {response.status_code}")
        try:
            data = response.json()
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
            usage = data.get("usage") or {}
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AIError(f"{self._url}: unexpected response shape") from exc
        return Completion(
            text,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            choice.get("finish_reason"),
        )

    def _redact(self, text: str) -> str:
        return redact(text, [self.slot.api_key])
