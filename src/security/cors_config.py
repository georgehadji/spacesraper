# src/security/cors_config.py
import os


def build_cors_origins() -> list[str]:
    """
    Returns the list of allowed CORS origins from the CORS_ALLOWED_ORIGINS
    environment variable (comma-separated).

    Falls back to localhost development origins if the env var is not set.
    Never returns ["*"] — wildcard origins are not permitted.

    Docker Compose usage:
        environment:
          CORS_ALLOWED_ORIGINS: "https://app.mycompany.com,https://dashboard.mycompany.com"
    """
    raw = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return [
        "http://localhost:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]
