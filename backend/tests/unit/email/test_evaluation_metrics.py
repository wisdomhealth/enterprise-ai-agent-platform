import pytest

from app.modules.email.classification import ClassificationExecution
from app.modules.email.evaluation import (
    EMAIL_CLASSIFICATION_METRICS_VERSION,
    EmailEvaluationCase,
    EmailEvaluationDataset,
    EmailEvaluationDatasetKind,
    EmailEvaluationRunner,
    calculate_macro_f1,
)
from app.modules.email.models import EmailCategory, EmailPriority
from app.modules.email.schemas import EmailClassification


def test_macro_f1_scores_each_category_instead_of_micro_averaging() -> None:
    expected = [
        EmailCategory.ACTION_REQUIRED,
        EmailCategory.ACTION_REQUIRED,
        EmailCategory.INFORMATIONAL,
        EmailCategory.SPAM,
    ]
    predicted = [
        EmailCategory.ACTION_REQUIRED,
        EmailCategory.INFORMATIONAL,
        EmailCategory.INFORMATIONAL,
        EmailCategory.SPAM,
    ]

    assert calculate_macro_f1(expected, predicted) == 0.7777777777777777


def test_macro_f1_is_zero_for_empty_evaluation_set() -> None:
    assert calculate_macro_f1([], []) == 0.0


class WrongPriorityAndReplyClassifier:
    async def classify(self, _subject: str, _body: str) -> ClassificationExecution:
        return ClassificationExecution(
            EmailClassification.model_construct(
                category=EmailCategory.ACTION_REQUIRED,
                priority=EmailPriority.LOW,
                reply_required=False,
            ),
            "fixture-model",
            "fixture-prompt",
            1,
            2,
            1,
            0.0,
        )


@pytest.mark.asyncio
async def test_quality_score_cannot_be_perfect_when_priority_and_reply_are_wrong() -> None:
    dataset = EmailEvaluationDataset(
        kind=EmailEvaluationDatasetKind.REGRESSION,
        version="email-regression-v2",
        digest="a" * 64,
        cases=[
            EmailEvaluationCase(
                message_id="message-1",
                subject="Please respond",
                body="This is urgent.",
                expected_category=EmailCategory.ACTION_REQUIRED,
                expected_priority=EmailPriority.HIGH,
                expected_reply_required=True,
            )
        ],
    )

    run = await EmailEvaluationRunner(WrongPriorityAndReplyClassifier()).run(dataset)

    assert run.metrics_version == EMAIL_CLASSIFICATION_METRICS_VERSION
    assert run.category_macro_f1 == 1.0
    assert run.priority_macro_f1 == 0.0
    assert run.reply_required_f1 == 0.0
    assert run.exact_match_rate == 0.0
    assert run.macro_f1 < 1.0
