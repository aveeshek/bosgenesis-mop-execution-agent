"""API runtime entrypoint."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    """Run the API server."""
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8080"))
    uvicorn.run(
        "bosgenesis_mop_execution_agent.api.app:create_app",
        factory=True,
        host=host,
        port=port,
    )


if __name__ == "__main__":
    main()
