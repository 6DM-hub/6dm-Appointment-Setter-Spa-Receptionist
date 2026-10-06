from fastapi.testclient import TestClient
from fastapi.middleware.cors import CORSMiddleware

from app.main import app
from app.core.config import parse_cors_origins


def test_cors_origin_parser_normalizes_platform_values():
    assert parse_cors_origins(
        '["https://frontend-ai-scheduler2.vercel.app/", "https://frontend-eight-ebon-78.vercel.app"]'
    ) == [
        "https://frontend-ai-scheduler2.vercel.app",
        "https://frontend-eight-ebon-78.vercel.app",
    ]


def test_auth_login_preflight_allows_production_frontend_origin(monkeypatch):
    cors = next(item for item in app.user_middleware if item.cls is CORSMiddleware)
    monkeypatch.setitem(
        cors.kwargs,
        "allow_origins",
        ["https://frontend-ai-scheduler2.vercel.app"],
    )
    app.middleware_stack = None
    response = TestClient(app).options(
        "/api/v1/auth/login",
        headers={
            "Origin": "https://frontend-ai-scheduler2.vercel.app",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == (
        "https://frontend-ai-scheduler2.vercel.app"
    )