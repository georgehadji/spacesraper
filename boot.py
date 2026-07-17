#!/usr/bin/env python3
"""
Spacescraper boot script — starts all processes with one command.

Usage:
    python boot.py                  # Start all processes
    python boot.py --api-only       # Start only the API server
    python boot.py --workers-only   # Start only the workers
"""

import asyncio
import signal
import sys
import logging
from typing import List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("Boot")


async def run_process(name: str, cmd: str, *args: str):
    """Run a subprocess and log its output."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, cmd, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    logger.info("Started %s (pid=%d)", name, proc.pid)

    async def read_output():
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            print(f"[{name}] {line.decode().rstrip()}")

    await asyncio.gather(read_output(), proc.wait())
    logger.warning("%s exited (code=%d)", name, proc.returncode)


async def main():
    processes = []

    # API server
    processes.append(("api", "main.py"))

    # Workers
    processes.append(("scraper", "worker_scraper.py"))
    processes.append(("processor", "worker_processor.py"))
    processes.append(("reporter", "worker_reporter.py"))

    # Parse CLI args
    args = set(sys.argv[1:])
    if "--api-only" in args:
        processes = [p for p in processes if p[0] == "api"]
    elif "--workers-only" in args:
        processes = [p for p in processes if p[0] != "api"]

    logger.info("Boot: Starting %d processes: %s", len(processes), [p[0] for p in processes])

    try:
        await asyncio.gather(*[run_process(name, cmd) for name, cmd in processes])
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Boot: Shutting down...")
