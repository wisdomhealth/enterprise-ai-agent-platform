from uuid import uuid4

import pytest

from app.modules.rag.evaluation import (
    AcceptanceDatasetUseError,
    EvaluatedClaim,
    EvaluationCase,
    EvaluationDataset,
    EvaluationDatasetKind,
    EvaluationUse,
    calculate_abstention_rate,
    calculate_answer_groundedness,
    calculate_claim_groundedness,
    calculate_recall_at_k,
    load_evaluation_dataset,
)


def test_claim_groundedness_scores_supported_claims_individually() -> None:
    claims = [
        EvaluatedClaim(text="A", supported=True),
        EvaluatedClaim(text="B", supported=False),
    ]

    assert calculate_claim_groundedness(claims) == 0.5


def test_recall_and_abstention_metrics_are_case_normalized() -> None:
    first_document = uuid4()
    second_document = uuid4()

    assert calculate_recall_at_k([first_document, second_document], [first_document], k=10) == 1.0
    assert calculate_recall_at_k([second_document], [first_document], k=10) == 0.0
    assert calculate_abstention_rate([(False, True), (False, False), (True, True)]) == 0.5
    assert calculate_answer_groundedness(
        [
            [EvaluatedClaim(text="Supported.", supported=True)],
            [EvaluatedClaim(text="Unsupported.", supported=False)],
        ]
    ) == 0.5


def test_held_out_acceptance_dataset_cannot_be_loaded_for_tuning(tmp_path) -> None:  # type: ignore[no-untyped-def]
    case = EvaluationCase(
        case_id="held-out-1",
        question="What is the refund policy?",
        answerable=True,
        authoritative_document_ids=[uuid4()],
        expected_claims=["Refunds take five business days."],
        forbidden_document_ids=[uuid4()],
        tags=["held-out"],
    )
    dataset = EvaluationDataset(
        kind=EvaluationDatasetKind.ACCEPTANCE,
        version="rag-acceptance-v1",
        cases=[case],
    )
    dataset_path = tmp_path / "acceptance.jsonl"
    dataset_path.write_text(dataset.to_jsonl(), encoding="utf-8")

    with pytest.raises(AcceptanceDatasetUseError):
        load_evaluation_dataset(dataset_path, use=EvaluationUse.TUNING)
