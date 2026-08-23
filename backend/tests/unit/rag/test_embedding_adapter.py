import pytest

from app.modules.rag.embeddings import OpenAIEmbeddingProvider


class FakeEmbeddingsAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str, int]] = []

    async def create(self, *, input: list[str], model: str, dimensions: int):  # type: ignore[no-untyped-def]
        self.calls.append((input, model, dimensions))
        return type(
            "Response",
            (),
            {
                "data": [
                    type("Embedding", (), {"index": index, "embedding": [float(index)] * 1536})
                    for index, _ in enumerate(input)
                ]
            },
        )()


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.embeddings = FakeEmbeddingsAPI()


@pytest.mark.asyncio
async def test_embedding_provider_uses_one_batched_call_and_preserves_order() -> None:
    client = FakeOpenAIClient()
    provider = OpenAIEmbeddingProvider(client)  # type: ignore[arg-type]

    vectors = await provider.embed(["first", "second"])

    assert client.embeddings.calls == [(["first", "second"], "text-embedding-3-small", 1536)]
    assert vectors == [[0.0] * 1536, [1.0] * 1536]
