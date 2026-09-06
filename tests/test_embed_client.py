import json

import httpx
import pytest
from conftest import EMBED_URL, register_fake_embeddings
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

from mentor.config import Settings
from mentor.embed.client import EmbeddingClient, EmbeddingError, pack


def test_batches_in_order(httpx_mock: HTTPXMock, settings: Settings) -> None:
    batches: list[list[str]] = []
    register_fake_embeddings(httpx_mock, batches)
    texts = [f"text {i}" for i in range(70)]

    with EmbeddingClient(settings) as client:
        vectors = client.embed(texts)

    assert [len(b) for b in batches] == [32, 32, 6]
    assert len(vectors) == 70 and vectors[0] == [0.0, 0.0, 0.0, 1.0]
    assert "authorization" not in httpx_mock.get_requests(url=EMBED_URL)[0].headers


def test_reversed_index_order_is_restored(httpx_mock: HTTPXMock, settings: Settings) -> None:
    httpx_mock.add_response(
        url=EMBED_URL,
        json={"data": [{"index": 1, "embedding": [1.0]}, {"index": 0, "embedding": [0.0]}]},
    )
    with EmbeddingClient(settings) as client:
        assert client.embed(["a", "b"]) == [[0.0], [1.0]]


def test_bearer_token_sent_only_with_key(httpx_mock: HTTPXMock, settings: Settings) -> None:
    httpx_mock.add_response(url=EMBED_URL, status_code=500)
    keyed = settings.model_copy(update={"embed_api_key": SecretStr("hunter2")})

    with EmbeddingClient(keyed) as client, pytest.raises(EmbeddingError) as excinfo:
        client.embed(["a"])

    assert httpx_mock.get_request(url=EMBED_URL).headers["authorization"] == "Bearer hunter2"
    assert "hunter2" not in str(excinfo.value) and "HTTP 500" in str(excinfo.value)
    assert json.loads(httpx_mock.get_request(url=EMBED_URL).read())["model"] == "nomic-embed-text"


def test_transport_error_names_the_url(httpx_mock: HTTPXMock, settings: Settings) -> None:
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=EMBED_URL)
    with EmbeddingClient(settings) as client, pytest.raises(EmbeddingError, match="ConnectError"):
        client.embed(["a"])


def test_count_mismatch_and_bad_shape(httpx_mock: HTTPXMock, settings: Settings) -> None:
    httpx_mock.add_response(url=EMBED_URL, json={"data": [{"index": 0, "embedding": [1.0]}]})
    httpx_mock.add_response(url=EMBED_URL, json={"nope": True})
    with EmbeddingClient(settings) as client:
        with pytest.raises(EmbeddingError, match="1 vectors for 2 inputs"):
            client.embed(["a", "b"])
        with pytest.raises(EmbeddingError, match="unexpected response shape"):
            client.embed(["a"])


def test_pack_is_float32_little_endian() -> None:
    assert pack([1.0, 0.0]) == b"\x00\x00\x80\x3f\x00\x00\x00\x00"
