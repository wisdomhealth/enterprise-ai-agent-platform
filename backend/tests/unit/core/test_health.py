from fastapi.testclient import TestClient

from app.main import create_app


def test_liveness_does_not_require_external_dependencies() -> None:
    response = TestClient(create_app()).get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
