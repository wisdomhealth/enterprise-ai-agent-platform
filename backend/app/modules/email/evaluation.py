from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email.classification import ClassificationExecution
from app.modules.email.models import (
    EmailCategory,
    EmailPriority,
)
from app.modules.email.models import (
    EmailEvaluationRun as EmailEvaluationRunRecord,
)


class EmailEvaluationDatasetKind(StrEnum):
    REGRESSION = "regression"
    ACCEPTANCE = "acceptance"


class EmailEvaluationUse(StrEnum):
    EVALUATION = "evaluation"
    TUNING = "tuning"


class AcceptanceDatasetUseError(ValueError):
    pass


class EmailEvaluationCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str = Field(min_length=1, max_length=512)
    subject: str = Field(min_length=1, max_length=4_000)
    body: str = Field(min_length=1, max_length=16_000)
    expected_category: EmailCategory
    expected_priority: EmailPriority
    expected_reply_required: bool


class EmailEvaluationDataset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EmailEvaluationDatasetKind
    version: str = Field(min_length=1, max_length=200)
    digest: str = Field(min_length=64, max_length=64)
    cases: list[EmailEvaluationCase]


class EmailEvaluationRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    dataset_version: str
    dataset_kind: EmailEvaluationDatasetKind
    dataset_digest: str
    model: str
    prompt_version: str
    macro_f1: float = Field(ge=0, le=1)
    structured_output_success: float = Field(ge=0, le=1)
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost: float = Field(ge=0)


class EvaluationClassifier(Protocol):
    async def classify(self, subject: str, body: str) -> ClassificationExecution: ...


def calculate_macro_f1(
    expected: Sequence[EmailCategory], predicted: Sequence[EmailCategory]
) -> float:
    if len(expected) != len(predicted):
        raise ValueError("expected and predicted classifications must have equal length")
    if not expected:
        return 0.0
    labels = set(expected) | set(predicted)
    scores: list[float] = []
    for label in labels:
        true_positive = sum(e is label and p is label for e, p in zip(expected, predicted))
        false_positive = sum(e is not label and p is label for e, p in zip(expected, predicted))
        false_negative = sum(e is label and p is not label for e, p in zip(expected, predicted))
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append(2 * true_positive / denominator if denominator else 0.0)
    return sum(scores) / len(scores)


def load_email_evaluation_dataset(
    path: Path, *, use: EmailEvaluationUse = EmailEvaluationUse.EVALUATION
) -> EmailEvaluationDataset:
    raw = path.read_bytes()
    records = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not records:
        raise ValueError("email evaluation dataset is empty")
    kinds = {record.pop("dataset_kind", None) for record in records}
    versions = {record.pop("dataset_version", None) for record in records}
    if len(kinds) != 1 or len(versions) != 1:
        raise ValueError("email evaluation metadata must be consistent")
    kind = EmailEvaluationDatasetKind(kinds.pop())
    version = versions.pop()
    if not isinstance(version, str) or not version:
        raise ValueError("email evaluation dataset version is required")
    if kind is EmailEvaluationDatasetKind.ACCEPTANCE and use is EmailEvaluationUse.TUNING:
        raise AcceptanceDatasetUseError("held-out email acceptance labels cannot tune a model")
    return EmailEvaluationDataset(
        kind=kind,
        version=version,
        digest=sha256(raw).hexdigest(),
        cases=[EmailEvaluationCase.model_validate(record) for record in records],
    )


class EmailEvaluationRepository:
    def __init__(self, db_session: AsyncSession) -> None:
        self._db_session = db_session

    async def append(self, run: EmailEvaluationRun) -> None:
        self._db_session.add(
            EmailEvaluationRunRecord(
                id=run.id,
                dataset_version=run.dataset_version,
                dataset_kind=run.dataset_kind.value,
                dataset_digest=run.dataset_digest,
                model=run.model,
                prompt_version=run.prompt_version,
                macro_f1=run.macro_f1,
                structured_output_success=run.structured_output_success,
                latency_ms=run.latency_ms,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                estimated_cost=run.estimated_cost,
            )
        )
        await self._db_session.flush()


class EmailEvaluationRunner:
    def __init__(
        self,
        classifier: EvaluationClassifier,
        repository: EmailEvaluationRepository | None = None,
    ) -> None:
        self._classifier = classifier
        self._repository = repository

    async def run(self, dataset: EmailEvaluationDataset) -> EmailEvaluationRun:
        expected: list[EmailCategory] = []
        predicted: list[EmailCategory] = []
        successful = 0
        latency_ms = input_tokens = output_tokens = 0
        estimated_cost = 0.0
        model = "unavailable"
        prompt_version = "unavailable"
        for case in dataset.cases:
            expected.append(case.expected_category)
            try:
                result = await self._classifier.classify(case.subject, case.body)
            except Exception:
                predicted.append(_always_wrong_label(case.expected_category))
                continue
            successful += 1
            predicted.append(result.classification.category)
            latency_ms += result.latency_ms
            input_tokens += result.input_tokens
            output_tokens += result.output_tokens
            estimated_cost += result.estimated_cost
            model = result.model
            prompt_version = result.prompt_version
        case_count = len(dataset.cases)
        run = EmailEvaluationRun(
            dataset_version=dataset.version,
            dataset_kind=dataset.kind,
            dataset_digest=dataset.digest,
            model=model,
            prompt_version=prompt_version,
            macro_f1=calculate_macro_f1(expected, predicted),
            structured_output_success=(successful / case_count if case_count else 0.0),
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=estimated_cost,
        )
        if self._repository is not None:
            await self._repository.append(run)
        return run


def _always_wrong_label(expected: EmailCategory) -> EmailCategory:
    return EmailCategory.SPAM if expected is not EmailCategory.SPAM else EmailCategory.INFORMATIONAL
