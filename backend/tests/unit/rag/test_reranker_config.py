from unittest.mock import sentinel

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.modules.rag.answer_service import GroundedAnswerService
from app.modules.rag.reranker import OpenAIReranker


def _settings(**overrides: object) -> Settings:
    values = {
        "OPENAI_API_KEY": "test-key",
        "REDIS_URL": "redis://localhost:6379/15",
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


def test_enabled_reranking_requires_a_model() -> None:
    with pytest.raises(ValidationError, match="RERANKER_MODEL"):
        _settings(RERANKER_ENABLED=True)


def test_disabled_reranking_does_not_construct_a_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        OpenAIReranker,
        "from_settings",
        classmethod(lambda cls, settings: pytest.fail("reranker must not be constructed")),
    )

    service = GroundedAnswerService.from_settings(_settings(RERANKER_ENABLED=False))

    retriever = service._workflow._context.retriever  # noqa: SLF001
    assert retriever._reranker is None  # type: ignore[attr-defined]  # noqa: SLF001


def test_enabled_reranking_injects_live_implementation_and_pool_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        OpenAIReranker,
        "from_settings",
        classmethod(lambda cls, settings: sentinel.reranker),
    )
    settings = _settings(
        RERANKER_ENABLED=True,
        RERANKER_MODEL="gpt-4.1-mini",
        RRF_CANDIDATE_POOL_SIZE=24,
        RERANKER_MAX_CANDIDATES=16,
        RERANKER_TIMEOUT_SECONDS=7,
        RERANKER_MAX_PASSAGE_TOKENS=300,
        RERANKER_MAX_INPUT_TOKENS=6_000,
    )

    service = GroundedAnswerService.from_settings(settings)

    retriever = service._workflow._context.retriever  # noqa: SLF001
    assert retriever._reranker is sentinel.reranker  # type: ignore[attr-defined]  # noqa: SLF001
    assert retriever._candidate_pool_size == 24  # type: ignore[attr-defined]  # noqa: SLF001
