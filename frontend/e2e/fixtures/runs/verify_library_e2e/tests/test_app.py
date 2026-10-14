import app
from fastapi.testclient import TestClient


def test_health() -> None:
    assert TestClient(app.app).get("/health").status_code == 200
