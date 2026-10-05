from app.core.config import Settings


def test_openai_models_costs_and_budgets_have_stable_defaults() -> None:
    settings = Settings()

    assert settings.openai_generation_model == "gpt-4.1-mini"
    assert settings.openai_classifier_model == "gpt-4.1-mini"
    assert settings.openai_embedding_model == "text-embedding-3-small"
    assert settings.openai_request_timeout_seconds == 30.0
    assert settings.rag_execution_timeout_seconds == 60.0
    assert settings.rag_max_generation_attempts == 2
    assert settings.openai_input_cost_per_million >= 0
    assert settings.openai_output_cost_per_million >= 0


def test_anthropic_is_not_a_runtime_setting() -> None:
    assert "anthropic_api_key" not in Settings.model_fields
    assert "anthropic_model" not in Settings.model_fields
