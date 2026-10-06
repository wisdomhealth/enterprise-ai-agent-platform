import math

import pytest

from app.core.config import Settings
from app.modules.rag.embeddings import OpenAIEmbeddingProvider


class FakeLlamaIndexEmbedding:
    def __init__(self, vectors: list[list[float]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._vectors = vectors

    async def aget_text_embedding_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self._vectors is not None:
            return self._vectors
        return [[float(index)] * 1536 for index, _ in enumerate(texts)]


@pytest.mark.asyncio
async def test_embedding_provider_uses_llamaindex_async_batch_and_preserves_order() -> None:
    embed_model = FakeLlamaIndexEmbedding()
    provider = OpenAIEmbeddingProvider(embed_model)

    vectors = await provider.embed(["first", "second"])

    assert embed_model.calls == [["first", "second"]]
    assert vectors == [[0.0] * 1536, [1.0] * 1536]


def test_embedding_provider_builds_llamaindex_adapter_from_settings(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_embedding(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return FakeLlamaIndexEmbedding()

    monkeypatch.setattr("app.modules.rag.embeddings.OpenAIEmbedding", fake_embedding)

    provider = OpenAIEmbeddingProvider.from_settings(
        Settings(
            OPENAI_API_KEY="secret",
            OPENAI_BASE_URL="https://openai.example/v1",
            OPENAI_EMBEDDING_MODEL="text-embedding-3-small",
            OPENAI_EMBEDDING_DIMENSIONS=1536,
            OPENAI_EMBEDDING_BATCH_SIZE=32,
            OPENAI_EMBEDDING_MAX_RETRIES=2,
            OPENAI_REQUEST_TIMEOUT_SECONDS=17.0,
        )
    )

    assert isinstance(provider, OpenAIEmbeddingProvider)
    assert captured == {
        "api_key": "secret",
        "api_base": "https://openai.example/v1",
        "model": "text-embedding-3-small",
        "dimensions": 1536,
        "embed_batch_size": 32,
        "max_retries": 2,
        "timeout": 17.0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vectors",
    (
        [[0.0] * 1536],
        [[0.0] * 8, [1.0] * 8],
        [[0.0] * 1536, [math.inf] * 1536],
    ),
)
async def test_embedding_provider_rejects_invalid_batches(vectors: list[list[float]]) -> None:
    provider = OpenAIEmbeddingProvider(FakeLlamaIndexEmbedding(vectors))

    with pytest.raises(ValueError, match="invalid vector batch"):
        await provider.embed(["first", "second"])
