"""Versioned, label-safe offline RAG evaluation primitives.

Evaluation labels are intentionally consumed only after an answer has been generated.
They are never supplied to retrieval, prompt construction, or a generation provider.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.dependencies import Principal
from app.modules.rag.answer_service import AnswerExecution
from app.modules.rag.evaluation_models import RAGEvaluationCase, RAGEvaluationRun
from app.modules.rag.types import (
    AnswerAudience,
    RetrievedChunk,
    SourceCitation,
    ValidatedAnswer,
)


class EvaluationDatasetKind(StrEnum):
    REGRESSION = "regression"
    ACCEPTANCE = "acceptance"


class EvaluationUse(StrEnum):
    EVALUATION = "evaluation"
    TUNING = "tuning"


class AcceptanceDatasetUseError(ValueError):
    """Held-out acceptance labels are not permitted as tuning input."""


class EvaluationCase(BaseModel):
    """A versioned record whose labels stay outside the prompt-building path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=4_000)
    answerable: bool
    authoritative_document_ids: list[UUID] = Field(default_factory=list)
    expected_claims: list[str] = Field(default_factory=list)
    forbidden_document_ids: list[UUID] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_answerability_contract(self) -> EvaluationCase:
        if self.answerable and not self.authoritative_document_ids:
            raise ValueError("answerable evaluation cases require authoritative_document_ids")
        if not self.answerable and self.expected_claims:
            raise ValueError("unanswerable evaluation cases cannot include expected_claims")
        return self


class EvaluationDataset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EvaluationDatasetKind
    version: str = Field(min_length=1, max_length=200)
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def _require_unique_cases(self) -> EvaluationDataset:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation case_id values must be unique")
        return self

    def to_jsonl(self) -> str:
        """Persist each record with its dataset identity for independently portable fixtures."""
        records = [
            {
                "dataset_kind": self.kind.value,
                "dataset_version": self.version,
                **case.model_dump(mode="json"),
            }
            for case in self.cases
        ]
        return "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)


def load_evaluation_dataset(
    path: Path | str,
    *,
    use: EvaluationUse = EvaluationUse.EVALUATION,
) -> EvaluationDataset:
    """Load a JSONL dataset while rejecting held-out labels for tuning."""
    records: list[dict[str, object]] = []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid evaluation JSONL at line {line_number}") from error
        if not isinstance(raw, dict):
            raise ValueError(f"evaluation JSONL line {line_number} must be an object")
        records.append(raw)
    if not records:
        raise ValueError("evaluation dataset cannot be empty")

    kinds = {record.get("dataset_kind") for record in records}
    versions = {record.get("dataset_version") for record in records}
    if len(kinds) != 1 or len(versions) != 1:
        raise ValueError("evaluation JSONL records must share one kind and version")
    kind_value = next(iter(kinds))
    version = next(iter(versions))
    if not isinstance(kind_value, str):
        raise ValueError("evaluation JSONL requires dataset_kind")
    try:
        kind = EvaluationDatasetKind(kind_value)
    except ValueError as error:
        raise ValueError("evaluation JSONL requires dataset_kind and dataset_version") from error
    if not isinstance(version, str) or not version:
        raise ValueError("evaluation JSONL requires a non-empty dataset_version")
    if kind is EvaluationDatasetKind.ACCEPTANCE and use is EvaluationUse.TUNING:
        raise AcceptanceDatasetUseError("held-out acceptance labels cannot be used for tuning")

    cases = [
        EvaluationCase.model_validate(
            {
                key: value
                for key, value in record.items()
                if key not in {"dataset_kind", "dataset_version"}
            }
        )
        for record in records
    ]
    return EvaluationDataset(kind=kind, version=version, cases=cases)


class EvaluatedClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    supported: bool


def calculate_recall_at_k(
    retrieved_document_ids: Sequence[UUID],
    authoritative_document_ids: Sequence[UUID],
    *,
    k: int = 10,
) -> float:
    if k < 1:
        raise ValueError("k must be positive")
    authoritative = set(authoritative_document_ids)
    if not authoritative:
        return 1.0
    return len(set(retrieved_document_ids[:k]) & authoritative) / len(authoritative)


def calculate_abstention_rate(outcomes: Sequence[tuple[bool, bool]]) -> float:
    """Correct-refusal rate for unanswerable cases, not a model quality gate."""
    unanswerable = [refused for answerable, refused in outcomes if not answerable]
    return sum(unanswerable) / len(unanswerable) if unanswerable else 1.0


def calculate_claim_groundedness(claims: Sequence[EvaluatedClaim]) -> float:
    return sum(claim.supported for claim in claims) / len(claims) if claims else 1.0


def calculate_answer_groundedness(claim_sets: Sequence[Sequence[EvaluatedClaim]]) -> float:
    if not claim_sets:
        return 1.0
    answer_scores = [calculate_claim_groundedness(claims) for claims in claim_sets]
    return sum(score == 1.0 for score in answer_scores) / len(answer_scores)


class EvaluationProvenance(BaseModel):
    """The immutable configuration identity attached to one evaluation run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_version_set: str = Field(min_length=1)
    chunking_version: str = Field(min_length=1)
    embedding_model: str = Field(min_length=1)
    retrieval_config: dict[str, str] = Field(default_factory=dict)
    prompt_version: str = Field(min_length=1)
    llm_model: str = Field(min_length=1)


class EvaluationMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recall_at_10: float = Field(ge=0, le=1)
    citation_mapping_rate: float = Field(ge=0, le=1)
    citation_support_rate: float = Field(ge=0, le=1)
    abstention_rate: float = Field(ge=0, le=1)
    answer_groundedness: float = Field(ge=0, le=1)
    claim_groundedness: float = Field(ge=0, le=1)
    retrieval_latency_ms: float = Field(ge=0)
    model_latency_ms: float = Field(ge=0)
    end_to_end_latency_ms: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost: float = Field(ge=0)


class HardGateStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    authorized_candidates_only: bool
    no_forbidden_documents: bool
    citations_map_to_retrieval: bool

    @property
    def passed(self) -> bool:
        return (
            self.authorized_candidates_only
            and self.no_forbidden_documents
            and self.citations_map_to_retrieval
        )


class EvaluationRun(BaseModel):
    """Portable stored result; callers may serialize this without prompt or answer text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    organization_id: UUID
    knowledge_base_id: UUID
    dataset_version: str
    dataset_kind: EvaluationDatasetKind
    dataset_digest: str = Field(min_length=64, max_length=64)
    document_version_set: str
    chunking_version: str
    embedding_model: str
    retrieval_config: dict[str, str]
    prompt_version: str
    llm_model: str
    status: str = "COMPLETED"
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metrics: EvaluationMetrics
    hard_gates: HardGateStatus


class AnswerService(Protocol):
    async def answer_with_evidence(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        audience: AnswerAudience,
    ) -> AnswerExecution: ...


class EvaluationCaseProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    question_digest: str = Field(min_length=64, max_length=64)
    answer_refused: bool
    retrieved_chunk_ids: list[UUID]
    retrieved_document_ids: list[UUID]
    retrieved_document_version_ids: list[UUID]
    citation_chunk_ids: list[UUID]
    citation_document_version_ids: list[UUID]


class EvaluationRunRepository:
    def __init__(self, db_session: AsyncSession) -> None:
        self._db_session = db_session

    async def append(self, run: EvaluationRun, cases: Sequence[EvaluationCaseProvenance]) -> None:
        self._db_session.add(
            RAGEvaluationRun(
                id=run.id,
                organization_id=run.organization_id,
                knowledge_base_id=run.knowledge_base_id,
                dataset_version=run.dataset_version,
                dataset_kind=run.dataset_kind.value,
                dataset_digest=run.dataset_digest,
                document_version_set=run.document_version_set,
                chunking_version=run.chunking_version,
                embedding_model=run.embedding_model,
                retrieval_config=run.retrieval_config,
                prompt_version=run.prompt_version,
                llm_model=run.llm_model,
                status=run.status,
                metrics=run.metrics.model_dump(mode="json"),
                hard_gates=run.hard_gates.model_dump(mode="json"),
                completed_at=run.completed_at,
            )
        )
        await self._db_session.flush()
        self._db_session.add_all(
            [RAGEvaluationCase(run_id=run.id, **case.model_dump()) for case in cases]
        )
        await self._db_session.flush()


class RAGEvaluationRunner:
    """Runs labels after answering, keeping them out of the generation input path."""

    def __init__(
        self, answer_service: AnswerService, repository: EvaluationRunRepository | None = None
    ) -> None:
        self._answer_service = answer_service
        self._repository = repository

    async def run(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        dataset: EvaluationDataset,
        provenance: EvaluationProvenance,
    ) -> EvaluationRun:
        recall_scores: list[float] = []
        citation_mapping_scores: list[float] = []
        citation_support_scores: list[float] = []
        abstention_outcomes: list[tuple[bool, bool]] = []
        evaluated_claim_sets: list[list[EvaluatedClaim]] = []
        retrieval_latencies: list[float] = []
        model_latencies: list[float] = []
        end_to_end_latencies: list[float] = []
        input_tokens = 0
        output_tokens = 0
        estimated_cost = 0.0
        authorized_candidates_only = True
        no_forbidden_documents = True
        citations_map_to_retrieval = True
        case_provenance: list[EvaluationCaseProvenance] = []

        for case in dataset.cases:
            started = time.monotonic()
            # Only the question crosses the answer boundary. Expected claims, authoritative
            # documents, forbidden documents, and tags never enter prompt construction.
            execution = await self._answer_service.answer_with_evidence(
                principal, knowledge_base_id, case.question, AnswerAudience.STAFF
            )
            answer = execution.answer
            chunks = execution.retrieved_chunks
            retrieval_latencies.append(float(execution.retrieval_latency_ms))
            end_to_end_latencies.append(_elapsed_ms(started))
            model_latencies.append(float(execution.model_latency_ms))
            input_tokens += answer.input_tokens
            output_tokens += answer.output_tokens
            estimated_cost += answer.estimated_cost

            retrieved_by_chunk = {chunk.chunk_id: chunk for chunk in chunks}
            retrieved_document_ids = [chunk.document_id for chunk in chunks]
            recall_scores.append(
                calculate_recall_at_k(retrieved_document_ids, case.authoritative_document_ids)
            )
            candidate_authorized = all(
                chunk.resource_authorized
                and chunk.retrieval_eligible
                and chunk.organization_id == principal.organization_id
                and chunk.knowledge_base_id == knowledge_base_id
                for chunk in chunks
            )
            candidate_forbidden = any(
                chunk.document_id in case.forbidden_document_ids for chunk in chunks
            )
            citation_chunks = _citation_chunks(answer, retrieved_by_chunk)
            citation_mapping = citation_chunks is not None
            cited_forbidden = citation_chunks is not None and any(
                chunk.document_id in case.forbidden_document_ids for chunk in citation_chunks
            )
            authorized_candidates_only = authorized_candidates_only and candidate_authorized
            no_forbidden_documents = no_forbidden_documents and not (
                candidate_forbidden or cited_forbidden
            )
            citations_map_to_retrieval = citations_map_to_retrieval and citation_mapping
            citation_mapping_scores.append(float(citation_mapping))

            expected_claims = {_normalized(item) for item in case.expected_claims}
            claims = [
                EvaluatedClaim(
                    text=claim.text,
                    supported=(
                        bool(claim.citation_ids)
                        and _normalized(claim.text) in expected_claims
                        and citation_mapping
                    ),
                )
                for claim in answer.claims
            ]
            if case.answerable:
                evaluated_claim_sets.append(claims or [EvaluatedClaim(text="", supported=False)])
                citation_support_scores.append(
                    float(bool(claims) and all(claim.supported for claim in claims))
                )
            abstention_outcomes.append((case.answerable, answer.refused))
            case_provenance.append(
                EvaluationCaseProvenance(
                    case_id=case.case_id,
                    question_digest=_digest(case.question),
                    answer_refused=answer.refused,
                    retrieved_chunk_ids=[chunk.chunk_id for chunk in chunks],
                    retrieved_document_ids=[chunk.document_id for chunk in chunks],
                    retrieved_document_version_ids=[chunk.document_version_id for chunk in chunks],
                    citation_chunk_ids=[
                        citation.chunk_id
                        for citation in answer.citations
                        if isinstance(citation, SourceCitation)
                    ],
                    citation_document_version_ids=[
                        citation.document_version_id
                        for citation in answer.citations
                        if isinstance(citation, SourceCitation)
                    ],
                )
            )

        metrics = EvaluationMetrics(
            recall_at_10=_mean(recall_scores),
            citation_mapping_rate=_mean(citation_mapping_scores),
            citation_support_rate=_mean(citation_support_scores),
            abstention_rate=calculate_abstention_rate(abstention_outcomes),
            answer_groundedness=calculate_answer_groundedness(evaluated_claim_sets),
            claim_groundedness=calculate_claim_groundedness(
                [claim for claims in evaluated_claim_sets for claim in claims]
            ),
            retrieval_latency_ms=_mean(retrieval_latencies),
            model_latency_ms=_mean(model_latencies),
            end_to_end_latency_ms=_mean(end_to_end_latencies),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=estimated_cost,
        )
        run = EvaluationRun(
            organization_id=principal.organization_id,
            knowledge_base_id=knowledge_base_id,
            dataset_version=dataset.version,
            dataset_kind=dataset.kind,
            dataset_digest=_digest(dataset.to_jsonl()),
            document_version_set=provenance.document_version_set,
            chunking_version=provenance.chunking_version,
            embedding_model=provenance.embedding_model,
            retrieval_config=provenance.retrieval_config,
            prompt_version=provenance.prompt_version,
            llm_model=provenance.llm_model,
            metrics=metrics,
            hard_gates=HardGateStatus(
                authorized_candidates_only=authorized_candidates_only,
                no_forbidden_documents=no_forbidden_documents,
                citations_map_to_retrieval=citations_map_to_retrieval,
            ),
        )
        if self._repository is not None:
            await self._repository.append(run, case_provenance)
        return run


def _citation_chunks(
    answer: ValidatedAnswer,
    retrieved_by_chunk: Mapping[UUID, RetrievedChunk],
) -> list[RetrievedChunk] | None:
    chunks: list[RetrievedChunk] = []
    for citation in answer.citations:
        if not isinstance(citation, SourceCitation):
            return None
        chunk = retrieved_by_chunk.get(citation.chunk_id)
        if chunk is None:
            return None
        chunks.append(chunk)
    return chunks


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.monotonic() - started) * 1_000)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
