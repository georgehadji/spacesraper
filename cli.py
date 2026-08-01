#!/usr/bin/env python3
"""
Spacescraper headless CLI.

A non-interactive entrypoint for external agents and scripts. Every command
writes a single JSON document to stdout and all diagnostics to stderr, so the
output can be piped straight into a parser.

Commands:
    extract   Extract records from local HTML. Fully offline and deterministic.
    scrape    Fetch a URL and extract from it. HTTP by default, --browser for JS.
    submit    Enqueue a job for the worker cluster (requires a live broker).
    health    Report dependency status.

Exit codes:
    0  success
    1  ran, but produced no records
    2  usage or input error
    3  fetch, network, or backend failure

Examples:
    python cli.py extract --html-file page.html --url https://example.com
    cat page.html | python cli.py extract --url https://example.com
    python cli.py scrape https://example.com --pretty
    python cli.py scrape https://example.com --browser --timeout 45
    python cli.py health
"""

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Dict, List, Optional

EXIT_OK = 0
EXIT_NO_RECORDS = 1
EXIT_USAGE = 2
EXIT_FAILURE = 3

logger = logging.getLogger("Spacescraper.CLI")


def _configure_logging(verbose: bool) -> None:
    """Send every log line to stderr so stdout stays pure JSON."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def _emit(document: Dict[str, Any], pretty: bool) -> None:
    json.dump(document, sys.stdout, indent=2 if pretty else None, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _serialize(entities: List[Any]) -> List[Dict[str, Any]]:
    records = []
    for entity in entities:
        if hasattr(entity, "model_dump"):
            records.append(entity.model_dump(mode="json"))
        elif isinstance(entity, dict):
            records.append(entity)
        else:
            records.append({"value": str(entity)})
    return records


def _load_overlay(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


async def _extract_from_html(
    html: str, url: str, overlay: Optional[Dict[str, Any]], job_id: str
) -> Dict[str, Any]:
    """Run the same extraction path the processor worker uses."""
    from src.application.pipeline import DataPipeline
    from src.domain.models import RawScrapePayload
    from src.extractors.universal_strategy import UniversalExtractionStrategy

    payload = RawScrapePayload(
        job_id=job_id,
        target_site="universal",
        url=url,
        status_code=200,
        html_content=html,
        json_payloads=[],
        overlay=overlay,
    )
    result = await DataPipeline(ai_enrichment_enabled=False).process(
        payload, UniversalExtractionStrategy()
    )
    return {
        "job_id": job_id,
        "url": url,
        "success": result.success,
        "error": result.error,
        "record_count": len(result.entities),
        "records": _serialize(result.entities),
        "follow_urls": result.follow_urls,
    }


async def _fetch_http(url: str, timeout: float) -> tuple[int, str]:
    import httpx

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True,
        headers={"User-Agent": "Spacescraper/2.5 (+headless-cli)"},
    ) as client:
        response = await client.get(url)
        return response.status_code, response.text


async def _fetch_browser(url: str, timeout: float) -> tuple[int, str]:
    from src.infrastructure.browser.pool import BrowserContextPool
    from src.infrastructure.browser.engine import ScraperEngine

    pool = BrowserContextPool(pool_size=1, headless=True)
    await pool.initialize()
    engine = ScraperEngine(context_pool=pool, timeout=int(timeout * 1000))
    try:
        await engine.start()
        payload = await engine.crawl(url)
        if payload.error_message:
            raise RuntimeError(payload.error_message)
        return payload.status_code, payload.html_content or ""
    finally:
        await engine.close()
        await pool.close_all()


# --- commands ---


async def cmd_extract(args: argparse.Namespace) -> int:
    if args.html_file:
        with open(args.html_file, "r", encoding="utf-8") as handle:
            html = handle.read()
    elif not sys.stdin.isatty():
        html = sys.stdin.read()
    else:
        print("error: provide --html-file or pipe HTML on stdin", file=sys.stderr)
        return EXIT_USAGE

    if not html.strip():
        print("error: empty HTML input", file=sys.stderr)
        return EXIT_USAGE

    result = await _extract_from_html(
        html, args.url, _load_overlay(args.overlay_file), args.job_id
    )
    _emit(result, args.pretty)
    return EXIT_OK if result["record_count"] else EXIT_NO_RECORDS


async def cmd_scrape(args: argparse.Namespace) -> int:
    from src.security.ssrf_guard import validate_outbound_url

    try:
        validate_outbound_url(args.url)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.browser:
            status_code, html = await _fetch_browser(args.url, args.timeout)
        else:
            status_code, html = await _fetch_http(args.url, args.timeout)
    except Exception as exc:
        _emit(
            {
                "job_id": args.job_id, "url": args.url, "success": False,
                "error": f"{type(exc).__name__}: {exc}",
                "record_count": 0, "records": [], "follow_urls": [],
            },
            args.pretty,
        )
        return EXIT_FAILURE

    result = await _extract_from_html(
        html, args.url, _load_overlay(args.overlay_file), args.job_id
    )
    result["status_code"] = status_code
    result["fetch_mode"] = "browser" if args.browser else "http"
    _emit(result, args.pretty)
    return EXIT_OK if result["record_count"] else EXIT_NO_RECORDS


async def cmd_submit(args: argparse.Namespace) -> int:
    import os
    import uuid

    from src.domain.models import ScrapeJob
    from src.infrastructure.queues.redis_worker import RedisQueueWorker

    queue = RedisQueueWorker(
        redis_url=args.redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379")
    )
    job_id = args.job_id or f"cli_{uuid.uuid4().hex[:8]}"
    try:
        await queue.connect()
        if queue._is_mock:
            # An in-process queue no worker can read would silently swallow the job.
            _emit(
                {
                    "submitted": False, "job_id": job_id, "url": args.url,
                    "error": "No live broker reachable; the job would be enqueued to a "
                             "private in-memory queue that no worker can read.",
                },
                args.pretty,
            )
            return EXIT_FAILURE

        await queue.push_job(
            "jobs_queue",
            ScrapeJob(job_id=job_id, url=args.url, target_site=args.site, overlay=_load_overlay(args.overlay_file)),
        )
        _emit(
            {"submitted": True, "job_id": job_id, "url": args.url, "target_site": args.site},
            args.pretty,
        )
        return EXIT_OK
    except Exception as exc:
        _emit(
            {"submitted": False, "job_id": job_id, "url": args.url, "error": str(exc)},
            args.pretty,
        )
        return EXIT_FAILURE
    finally:
        await queue.close()


async def cmd_health(args: argparse.Namespace) -> int:
    import os

    checks: Dict[str, Any] = {}

    # Extraction kernel: the only hard requirement for offline use.
    try:
        probe = await _extract_from_html(
            '<html><body><article><h2>Health Probe Record</h2><p>ok</p></article></body></html>',
            "https://healthcheck.local", None, "health",
        )
        checks["extraction"] = {"ok": probe["record_count"] > 0, "records": probe["record_count"]}
    except Exception as exc:
        checks["extraction"] = {"ok": False, "error": str(exc)}

    # Broker: optional; workers fall back to an in-memory queue.
    try:
        import valkey.asyncio as valkey

        client = valkey.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True
        )
        await client.ping()
        await client.aclose()
        checks["broker"] = {"ok": True, "mode": "live"}
    except Exception as exc:
        checks["broker"] = {"ok": False, "mode": "offline-fallback", "error": str(exc)}

    # Browser: optional; only needed for --browser scrapes.
    playwright = None
    try:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        version = browser.version
        await browser.close()
        checks["browser"] = {"ok": True, "chromium": version}
    except Exception as exc:
        checks["browser"] = {
            "ok": False,
            "error": str(exc).splitlines()[0],
            "hint": "run: python -m playwright install chromium",
        }
    finally:
        # Always stop the driver: a leaked subprocess transport produces noisy
        # "Event loop is closed" tracebacks on Windows at interpreter shutdown.
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass

    required_ok = checks["extraction"]["ok"]
    _emit(
        {
            "healthy": required_ok,
            "required": ["extraction"],
            "optional": ["broker", "browser"],
            "checks": checks,
        },
        args.pretty,
    )
    return EXIT_OK if required_ok else EXIT_FAILURE


def build_parser() -> argparse.ArgumentParser:
    # Shared flags are attached to every subcommand as well as the top level, so
    # both `cli.py --pretty health` and `cli.py health --pretty` work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pretty", action="store_true", help="Indent the JSON output.")
    common.add_argument("-v", "--verbose", action="store_true", help="Debug logging on stderr.")

    parser = argparse.ArgumentParser(
        prog="spacescraper",
        description="Headless Spacescraper CLI. Emits JSON on stdout, logs on stderr.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract", help="Extract records from local HTML.", parents=[common])
    extract.add_argument("--html-file", help="Path to an HTML file (default: read stdin).")
    extract.add_argument("--url", default="https://local.invalid", help="URL to attribute records to.")
    extract.add_argument("--overlay-file", help="JSON file with a declarative extraction overlay.")
    extract.add_argument("--job-id", default="cli_extract", help="Identifier echoed in the output.")
    extract.set_defaults(func=cmd_extract)

    scrape = sub.add_parser("scrape", help="Fetch a URL and extract records from it.", parents=[common])
    scrape.add_argument("url", help="Target URL, including scheme.")
    scrape.add_argument("--browser", action="store_true", help="Use headless Chromium instead of plain HTTP.")
    scrape.add_argument("--timeout", type=float, default=30.0, help="Fetch timeout in seconds.")
    scrape.add_argument("--overlay-file", help="JSON file with a declarative extraction overlay.")
    scrape.add_argument("--job-id", default="cli_scrape", help="Identifier echoed in the output.")
    scrape.set_defaults(func=cmd_scrape)

    submit = sub.add_parser("submit", help="Enqueue a job for the worker cluster.", parents=[common])
    submit.add_argument("url", help="Target URL, including scheme.")
    submit.add_argument("--site", default="universal", help="Extraction strategy identifier.")
    submit.add_argument("--overlay-file", help="JSON file with a declarative extraction overlay.")
    submit.add_argument("--job-id", help="Explicit job ID (default: generated).")
    submit.add_argument("--redis-url", help="Broker URL (default: $REDIS_URL).")
    submit.set_defaults(func=cmd_submit)

    health = sub.add_parser("health", help="Report dependency status.", parents=[common])
    health.set_defaults(func=cmd_health)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return EXIT_FAILURE
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON in overlay file: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
