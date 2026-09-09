"""Client for an OpenAI-compatible embeddings endpoint.

Talks to ``ORRERY_EMBED_BASE_URL`` and nothing else. The API key, when set, is sent as a
Bearer token and never appears in an exception message. No model-specific prompt prefixes
are added, so the endpoint and model stay swappable.
"""

import struct

import httpx

from orrery import __version__
from orrery.config import Settings


class EmbeddingError(Exception):
    """An embeddings request failed. The message never contains the API key."""


class EmbeddingClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self._url = settings.embed_base_url.rstrip("/") + "/embeddings"
        self._model = settings.embed_model
        self._key = settings.embed_api_key
        self._batch = settings.embed_batch_size
        headers = {"User-Agent": f"orrery/{__version__}"}
        if self._key is not None:
            headers["Authorization"] = f"Bearer {self._key.get_secret_value()}"
        self._http = http or httpx.Client(timeout=120.0, headers=headers)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "EmbeddingClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Vectors in input order, requested in batches of ``embed_batch_size``."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch):
            vectors.extend(self._request(texts[start : start + self._batch]))
        return vectors

    def _request(self, batch: list[str]) -> list[list[float]]:
        try:
            response = self._http.post(self._url, json={"model": self._model, "input": batch})
        except httpx.HTTPError as exc:
            message = self._redact(f"{self._url}: {type(exc).__name__}: {exc}")
            raise EmbeddingError(message) from exc
        if not response.is_success:
            raise EmbeddingError(f"{self._url}: HTTP {response.status_code}")
        try:
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            vectors = [[float(x) for x in item["embedding"]] for item in data]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"{self._url}: unexpected response shape") from exc
        if len(vectors) != len(batch):
            raise EmbeddingError(f"{self._url}: {len(vectors)} vectors for {len(batch)} inputs")
        return vectors

    def _redact(self, text: str) -> str:
        if self._key is None:
            return text
        return text.replace(self._key.get_secret_value(), "[embed_api_key]")


def pack(vector: list[float]) -> bytes:
    """float32 little-endian, the layout sqlite-vec reads."""
    return struct.pack(f"<{len(vector)}f", *vector)
