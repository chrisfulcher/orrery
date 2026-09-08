"""The Anthropic backend. pytest-httpx does not see the SDK's httpx2 traffic, so every test
injects a fake client or the SDK's own mock transport; conftest points ANTHROPIC_BASE_URL at
a closed port so a stray real call fails fast."""

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from anthropic import DefaultHttpxClient
from pydantic import SecretStr

from mentor import ai
from mentor.ai import AIError, InvalidResponse, Slot
from mentor.ai.anthropic_backend import FALLBACK_BETA, AnthropicBackend


def slot(**overrides: object) -> Slot:
    base = dict(
        name="deep", provider="anthropic", base_url=None, model="claude-opus-5",
        api_key=SecretStr("sk-test"), timeout=5.0, context_chars=200_000,
    )  # fmt: skip
    base.update(overrides)
    return Slot(**base)


def message(text: str, *, stop: str = "end_turn", category: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        stop_details=SimpleNamespace(category=category) if category else None,
        usage=SimpleNamespace(input_tokens=1_500, output_tokens=300),
    )


class FakeClient:
    def __init__(self, response: object | Exception) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.response = response
        self.messages = SimpleNamespace(create=lambda **kw: self._create("messages", kw))
        self.beta = SimpleNamespace(
            messages=SimpleNamespace(create=lambda **kw: self._create("beta", kw))
        )

    def _create(self, path: str, kwargs: dict) -> object:
        self.calls.append((path, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_opus_5_uses_the_fallback_beta_and_json_schema() -> None:
    client = FakeClient(message('{"fit": 80}'))
    completion = AnthropicBackend(slot(), client).complete(
        system="sys", user="usr", schema={"type": "object"}, max_tokens=16_000
    )
    assert completion.text == '{"fit": 80}' and completion.input_tokens == 1_500
    assert completion.output_tokens == 300 and completion.stop_reason == "end_turn"
    [(path, kwargs)] = client.calls
    assert (
        path == "beta" and kwargs["betas"] == [FALLBACK_BETA] and kwargs["fallbacks"] == "default"
    )
    assert kwargs["model"] == "claude-opus-5" and kwargs["max_tokens"] == 16_000
    assert (
        kwargs["system"][0]["cache_control"] == {"type": "ephemeral"} and "thinking" not in kwargs
    )
    assert kwargs["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}}
    }
    assert kwargs["messages"] == [{"role": "user", "content": "usr"}]


def test_other_models_use_the_plain_messages_endpoint() -> None:
    client = FakeClient(message("{}"))
    AnthropicBackend(slot(model="claude-sonnet-5"), client).complete(
        system="s", user="u", schema={}, max_tokens=8
    )
    assert client.calls[0][0] == "messages" and "fallbacks" not in client.calls[0][1]


def test_refusal_and_max_tokens_become_errors() -> None:
    backend = AnthropicBackend(slot(), FakeClient(message("", stop="refusal", category="cyber")))
    with pytest.raises(InvalidResponse, match="declined the request \\(cyber\\)"):
        backend.complete(system="s", user="u", schema={}, max_tokens=8)
    backend = AnthropicBackend(slot(), FakeClient(message("{", stop="max_tokens")))
    with pytest.raises(InvalidResponse, match="ran out of output tokens"):
        backend.complete(system="s", user="u", schema={}, max_tokens=8)


def _status_error(status: int, cls: type[anthropic.APIStatusError]) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(
        status, request=request, json={"error": {"message": "sk-test leaked?"}}
    )
    return cls("sk-test leaked?", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_status_error(401, anthropic.AuthenticationError), "authentication failed"),
        (_status_error(429, anthropic.RateLimitError), "rate limited"),
        (_status_error(500, anthropic.InternalServerError), "HTTP 500"),
        (
            anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            ),
            "APIConnectionError",
        ),
    ],
)
def test_sdk_errors_become_ai_errors_without_the_key(error: Exception, expected: str) -> None:
    backend = AnthropicBackend(slot(), FakeClient(error))
    with pytest.raises(AIError, match=expected) as raised:
        backend.complete(system="s", user="u", schema={}, max_tokens=8)
    assert "sk-test" not in str(raised.value)


def test_real_request_shape_through_the_sdk() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": '{"fit": 61}'}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },  # fmt: skip
        )

    client = anthropic.Anthropic(
        api_key="sk-test", http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    )
    completion = AnthropicBackend(slot(), client).complete(
        system="sys", user="usr", schema={"type": "object"}, max_tokens=64
    )
    assert completion.text == '{"fit": 61}' and completion.input_tokens == 12
    [request] = seen
    assert request.url.path == "/v1/messages" and request.headers["x-api-key"] == "sk-test"
    assert FALLBACK_BETA in request.headers.get("anthropic-beta", "")
    body = json.loads(request.content)
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["fallbacks"] == "default" and body["max_tokens"] == 64


def test_backend_for_builds_the_client_with_the_slot_key(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[dict] = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: made.append(kw) or object())
    backend = ai.backend_for(slot(base_url="https://gateway.example/v1"))
    assert isinstance(backend, AnthropicBackend)
    assert made[0]["api_key"] == "sk-test" and made[0]["base_url"] == "https://gateway.example/v1"
    assert made[0]["timeout"] == 5.0
    ai.backend_for(slot(api_key=None, base_url=None))
    assert "api_key" not in made[1] and "base_url" not in made[1]  # the SDK resolves its own
