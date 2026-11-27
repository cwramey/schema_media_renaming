"""``python -m schema_app`` / ``schema``: run the app with Uvicorn on SCHEMA_HOST:SCHEMA_PORT."""

from __future__ import annotations

import uvicorn

from schema_app.config import get_config


def main() -> None:
    config = get_config()
    uvicorn.run(
        "schema_app.main:create_app",
        factory=True,
        host=config.host,
        port=config.port,
        log_level=config.log_level,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
