"""Chat models behind one interface: a local model by default, any service by choice.

Two slots (DESIGN.md §4, AI task tiering): ``fast`` for grunt work, ``deep`` for judgment.
Each names a provider (``openai`` for any OpenAI-compatible endpoint: Ollama, LM Studio,
llama.cpp, vLLM, OpenAI, OpenRouter and the like; ``anthropic`` for the official SDK), a
base URL, a model, and an optional key. When no deep slot is configured, the deep slot is
the fast slot. Backends return text; ``complete_structured`` turns it into a validated
pydantic model, retrying once with the validation error. Keys never appear in messages.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, SecretStr, ValidationError

from mentor.config import Settings

SlotName = Literal["fast", "deep"]
Provider = Literal["openai", "anthropic"]
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5"


class AIError(Exception):
    """A model request failed. The message never contains a key."""


class InvalidResponse(AIError):
    """The model answered, but the answer is unusable: not the schema after a retry, a refusal,
    or an output cut off at ``max_tokens``. A walk over many items records it and moves on;
    ``completions`` are the attempts made, for token accounting."""

    def __init__(self, message: str, completions: Sequence["Completion"] = ()) -> None:
        super().__init__(message)
        self.completions = list(completions)


@dataclass(frozen=True)
class Slot:
    name: str
    provider: str
    base_url: str | None
    model: str
    api_key: SecretStr | None
    timeout: float
    context_chars: int
    """The prompt budget in characters (about four characters per token)."""


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    stop_reason: str | None


class ChatBackend(Protocol):
    slot: Slot

    def complete(self, *, system: str, user: str, schema: dict, max_tokens: int) -> Completion: ...

    def close(self) -> None: ...


def resolve_slot(settings: Settings, name: SlotName) -> Slot:
    """The configured slot; ``deep`` falls back to ``fast`` when no deep provider is set."""
    if name == "deep" and settings.ai_deep_provider is None:
        return resolve_slot(settings, "fast")
    if name == "fast":
        return Slot(
            "fast",
            settings.ai_fast_provider,
            settings.ai_fast_base_url,
            settings.ai_fast_model,
            settings.ai_fast_api_key,
            settings.ai_timeout,
            settings.ai_fast_context_chars,
        )
    provider = settings.ai_deep_provider
    assert provider is not None
    model = settings.ai_deep_model
    if model is None:
        if provider != "anthropic":
            raise AIError("MENTOR_AI_DEEP_MODEL is required when MENTOR_AI_DEEP_PROVIDER is openai")
        model = ANTHROPIC_DEFAULT_MODEL
    base_url = settings.ai_deep_base_url
    if base_url is None and provider == "openai":
        base_url = settings.ai_fast_base_url
    return Slot(
        "deep",
        provider,
        base_url,
        model,
        settings.ai_deep_api_key,
        settings.ai_timeout,
        settings.ai_deep_context_chars or settings.ai_fast_context_chars,
    )


def backend_for(slot: Slot) -> ChatBackend:
    if slot.provider == "openai":
        from mentor.ai.openai_compat import OpenAIChatBackend

        return OpenAIChatBackend(slot)
    if slot.provider == "anthropic":
        try:
            from mentor.ai.anthropic_backend import AnthropicBackend
        except ImportError as exc:
            raise AIError(
                "the anthropic provider needs the SDK: install the extra with"
                " `uv sync --extra anthropic` (or `pip install 'mentor[anthropic]'`)"
            ) from exc
        return AnthropicBackend(slot)
    raise AIError(f"unknown provider {slot.provider!r}")


THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_thinking(text: str) -> str:
    """Drop a reasoning preamble some local models emit, then keep the outermost JSON object."""
    text = THINK_BLOCK.sub("", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def complete_structured[M: BaseModel](
    backend: ChatBackend,
    *,
    system: str,
    user: str,
    model_type: type[M],
    max_tokens: int,
) -> tuple[M, list[Completion]]:
    """Ask for JSON matching ``model_type`` and validate it; one retry carries the problem
    back to the model. Returns the model and every completion made (for token accounting)."""
    schema = model_type.model_json_schema()
    completions: list[Completion] = []
    prompt = user
    error = ""
    for _attempt in range(2):
        completion = backend.complete(
            system=system, user=prompt, schema=schema, max_tokens=max_tokens
        )
        completions.append(completion)
        try:
            return model_type.model_validate_json(strip_thinking(completion.text)), completions
        except ValidationError as exc:
            error = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '(document)'}: {e['msg']}"
                for e in exc.errors()
            )
        except ValueError as exc:  # not JSON at all
            error = str(exc)
        prompt = (
            f"{user}\n\nYour previous answer was invalid: {error}."
            " Return only a JSON object matching the schema."
        )
    raise InvalidResponse(
        f"{backend.slot.model} did not return valid JSON after 2 attempts: {error}", completions
    )


def redact(text: str, keys: Iterable[SecretStr | None]) -> str:
    for key in keys:
        if key is not None and key.get_secret_value():
            text = text.replace(key.get_secret_value(), "[api_key]")
    return text
