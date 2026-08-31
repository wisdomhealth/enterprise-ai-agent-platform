from app.modules.email.evaluation import calculate_macro_f1
from app.modules.email.models import EmailCategory


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
