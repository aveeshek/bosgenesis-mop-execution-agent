"""Worker runtime entrypoint placeholder."""

from __future__ import annotations

import os
import time


def main() -> None:
    """Run a lightweight worker placeholder until Phase 7 adds the real loop."""
    interval_seconds = int(os.getenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "60"))
    while True:
        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
