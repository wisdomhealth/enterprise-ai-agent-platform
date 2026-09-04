FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
RUN python -m pip install --no-cache-dir .

FROM base AS test
COPY tests ./tests
RUN python -m pip install --no-cache-dir ".[dev]"
CMD ["python", "-m", "pytest", "tests/unit", "-q"]

FROM base AS runtime
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
