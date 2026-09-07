import json

import httpx
import pytest
from conftest import CHAT_URL, register_fake_chat
from pydantic import BaseModel, SecretStr
from pytest_httpx import HTTPXMock

from mentor import ai
from mentor.ai import AIError, Completion, Slot, complete_structured, resolve_slot, strip_thinking
from mentor.ai.openai_compat import OpenAIChatBackend
from mentor.config import Settings


class Verdict(BaseModel):
    fit: int
    reasons: list[str]


def slot(**overrides: object) -> Slot:
    base = dict(
        name="fast", provider="openai", base_url="http://localhost:11434/v1", model="qwen3:14b",
        api_key=None, timeout=5.0, context_chars=48_000,
    )  # fmt: skip
    base.update(overrides)
    return Slot(**base)


def test_deep_slot_falls_back_to_fast_and_anthropic_defaults() -> None:
    settings = Settings(_env_file=None)
    assert resolve_slot(settings, "deep") == resolve_slot(settings, "fast")
    deep = resolve_slot(Settings(_env_file=None, ai_deep_provider="anthropic"), "deep")
    assert (deep.name, deep.model, deep.base_url, deep.context_chars) == (
        "deep", "claude-opus-5", None, 48_000,
    )  # fmt: skip
    openai_deep = resolve_slot(
        Settings(
            _env_file=None,
            ai_deep_provider="openai",
            ai_deep_model="big",
            ai_deep_context_chars=90_000,
        ),
        "deep",
    )
    assert (openai_deep.base_url, openai_deep.context_chars) == (
        "http://localhost:11434/v1",
        90_000,
    )
    with pytest.raises(AIError, match="MENTOR_AI_DEEP_MODEL"):
        resolve_slot(Settings(_env_file=None, ai_deep_provider="openai"), "deep")


def test_request_shape_bearer_and_usage(httpx_mock: HTTPXMock) -> None:
    requests: list[dict] = []
    register_fake_chat(httpx_mock, ['{"fit": 70, "reasons": ["a"]}'], requests)
    with OpenAIChatBackend(slot(api_key=SecretStr("sekret"))) as backend:
        completion = backend.complete(
            system="be brief", user="assess", schema=Verdict.model_json_schema(), max_tokens=512
        )
    assert completion == Completion('{"fit": 70, "reasons": ["a"]}', 120, 40, "stop")
    sent = httpx_mock.get_request()
    assert sent.headers["authorization"] == "Bearer sekret"
    body = requests[0]
    assert body["model"] == "qwen3:14b" and body["max_tokens"] == 512
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["response_format"]["json_schema"]["schema"]["required"] == ["fit", "reasons"]
    assert body["response_format"]["json_schema"]["strict"] is True


def test_no_key_means_no_authorization_header(httpx_mock: HTTPXMock) -> None:
    register_fake_chat(httpx_mock, ["{}"], [])
    with OpenAIChatBackend(slot()) as backend:
        backend.complete(system="s", user="u", schema={}, max_tokens=8)
    assert "authorization" not in httpx_mock.get_request().headers


def test_errors_name_the_url_and_never_the_key(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=CHAT_URL, status_code=500)
    with OpenAIChatBackend(slot(api_key=SecretStr("sekret"))) as backend:
        with pytest.raises(AIError, match="HTTP 500"):
            backend.complete(system="s", user="u", schema={}, max_tokens=8)
    httpx_mock.add_exception(httpx.ConnectError("boom sekret"), url=CHAT_URL)
    with OpenAIChatBackend(slot(api_key=SecretStr("sekret"))) as backend:
        with pytest.raises(AIError) as raised:
            backend.complete(system="s", user="u", schema={}, max_tokens=8)
    assert "sekret" not in str(raised.value) and "ConnectError" in str(raised.value)
    with pytest.raises(AIError, match="no base URL"):
        OpenAIChatBackend(slot(base_url=None))


def test_strip_thinking() -> None:
    assert strip_thinking('<think>\nhmm\n</think>\n{"a": 1}') == '{"a": 1}'
    assert strip_thinking('Sure! ```json\n{"a": {"b": 2}}\n```') == '{"a": {"b": 2}}'
    assert strip_thinking("no json here") == "no json here"


def test_complete_structured_retries_once_with_the_error(httpx_mock: HTTPXMock) -> None:
    requests: list[dict] = []
    register_fake_chat(
        httpx_mock, ['{"fit": "high", "reasons": []}', '{"fit": 55, "reasons": ["ok"]}'], requests
    )
    with OpenAIChatBackend(slot()) as backend:
        verdict, completions = complete_structured(
            backend, system="s", user="assess this", model_type=Verdict, max_tokens=64
        )
    assert verdict == Verdict(fit=55, reasons=["ok"]) and len(completions) == 2
    second = requests[1]["messages"][1]["content"]
    assert second.startswith("assess this") and "previous answer was invalid: fit:" in second


def test_complete_structured_gives_up_after_two(httpx_mock: HTTPXMock) -> None:
    register_fake_chat(httpx_mock, ["not json"], [])
    with OpenAIChatBackend(slot()) as backend:
        with pytest.raises(AIError, match="after 2 attempts"):
            complete_structured(backend, system="s", user="u", model_type=Verdict, max_tokens=8)
    assert len(httpx_mock.get_requests()) == 2


def test_backend_for_anthropic_without_the_sdk_explains(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def missing(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("mentor.ai.anthropic_backend") or name == "anthropic":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(AIError, match="uv add anthropic"):
        ai.backend_for(slot(provider="anthropic"))
    with pytest.raises(AIError, match="unknown provider"):
        ai.backend_for(slot(provider="nope"))
    assert isinstance(ai.backend_for(slot()), OpenAIChatBackend)


def test_fake_chat_records_json_bodies(httpx_mock: HTTPXMock) -> None:
    requests: list[dict] = []
    register_fake_chat(httpx_mock, ["{}"], requests)
    httpx.post(CHAT_URL, json={"model": "x"})
    assert json.loads(json.dumps(requests[0])) == {"model": "x"}
