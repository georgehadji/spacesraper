#!/usr/bin/env python3
"""
Spacescraper headless CLI.

A non-interactive entrypoint for external agents and scripts. Every command
writes a single JSON document to stdout and all diagnostics to stderr, so the
output can be piped straight into a parser.

Commands:
    extract   Extract records from local HTML. Fully offline and deterministic.
    scrape    Fetch a URL and extract from it. HTTP by default, --browser for JS.
    map       Discover URLs via a site's sitemap(s), no extraction.
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


def _emit(document: dict[str, Any], pretty: bool) -> None:
    json.dump(document, sys.stdout, indent=2 if pretty else None, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _serialize(entities: list[Any]) -> list[dict[str, Any]]:
    records = []
    for entity in entities:
        if hasattr(entity, "model_dump"):
            records.append(entity.model_dump(mode="json"))
        elif isinstance(entity, dict):
            records.append(entity)
        else:
            records.append({"value": str(entity)})
    return records


def _load_overlay(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


async def _extract_from_html(
    html: str, url: str, overlay: dict[str, Any] | None, job_id: str,
    status_code: int = 200,
) -> dict[str, Any]:
    """Run the same extraction path the processor worker uses."""
    from src.application.extraction_pipeline import DeterministicExtractionPipeline, ExtractionPipeline
    from src.domain.models import RawScrapePayload

    payload = RawScrapePayload(
        job_id=job_id,
        target_site="universal",
        url=url,
        status_code=status_code,
        html_content=html,
        json_payloads=[],
        overlay=overlay,
    )
    result = await ExtractionPipeline().process(
        payload, DeterministicExtractionPipeline()
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
    from src.infrastructure.http_client import target_http

    response = await target_http.get(url, timeout=timeout)
    return response.status_code, response.text


async def _fetch_browser(url: str, timeout: float) -> tuple[int, str]:
    from src.infrastructure.browser.engine import ScraperEngine
    from src.infrastructure.browser.pool import BrowserContextPool

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
        with open(args.html_file, encoding="utf-8") as handle:
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
        html, args.url, _load_overlay(args.overlay_file), args.job_id, status_code
    )
    result["status_code"] = status_code
    result["fetch_mode"] = "browser" if args.browser else "http"
    _emit(result, args.pretty)
    return EXIT_OK if result["record_count"] else EXIT_NO_RECORDS


async def cmd_map(args: argparse.Namespace) -> int:
    from src.application.sitemap_seeder import discover_sitemap_urls
    from src.security.ssrf_guard import validate_outbound_url

    try:
        validate_outbound_url(args.url)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        urls = await discover_sitemap_urls(args.url, max_urls=args.max_urls)
    except Exception as exc:
        _emit({"url": args.url, "success": False, "error": f"{type(exc).__name__}: {exc}", "urls": []}, args.pretty)
        return EXIT_FAILURE

    _emit({"url": args.url, "success": True, "count": len(urls), "urls": urls}, args.pretty)
    return EXIT_OK if urls else EXIT_NO_RECORDS


async def cmd_submit(args: argparse.Namespace) -> int:
    import os
    import uuid

    from src.domain.models import MessageType, ScrapeJob
    from src.infrastructure.queues.stream_queue import ValkeyStreamQueue, make_message

    queue = ValkeyStreamQueue(
        valkey_url=args.valkey_url or os.environ.get("VALKEY_URL", "valkey://localhost:6379")
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

        job = ScrapeJob(job_id=job_id, url=args.url, target_site=args.site, overlay=_load_overlay(args.overlay_file))
        await queue.push(
            "jobs_stream",
            make_message(MessageType.SCRAPE_JOB, job.model_dump(mode="json"), root_job_id=job_id),
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

    checks: dict[str, Any] = {}

    # Extraction kernel: the only hard requirement for offline use.
    try:
        probe = await _extract_from_html(
            '<html><body><article><h2>Health Probe Record</h2>'
            '<p>Synthetic content long enough to clear the semantic-HTML minimum-length '
            'gate so this probe exercises a real extraction, not a length-filtered no-op.</p>'
            '</article></body></html>',
            "https://healthcheck.local", None, "health",
        )
        checks["extraction"] = {"ok": probe["record_count"] > 0, "records": probe["record_count"]}
    except Exception as exc:
        checks["extraction"] = {"ok": False, "error": str(exc)}

    # Broker: optional; workers fall back to an in-memory queue.
    try:
        import valkey.asyncio as valkey

        client = valkey.from_url(
            os.environ.get("VALKEY_URL", "valkey://localhost:6379"), decode_responses=True
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


async def cmd_places(args: argparse.Namespace) -> int:
    """Sweep named areas via the Places API and split by real web presence."""
    import os

    from src.application.place_sweep import (
        DEFAULT_DOCTOR_QUERIES,
        THERMAIKOS_AREAS,
        TYPE_PRESETS,
        AreaSpec,
        SweepConfig,
        run_places_sweep,
    )
    from src.infrastructure.places.google_places import (
        GooglePlacesClient,
        PlacesApiError,
    )

    api_key = args.api_key or os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        print(
            "error: no API key. Pass --api-key or set GOOGLE_MAPS_API_KEY "
            "(Places API New must be enabled on the key's project).",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if args.area:
        areas = [
            AreaSpec(name=a, query=a, radius_m=args.radius or 2000.0) for a in args.area
        ]
    else:
        areas = [
            AreaSpec(
                name=a.name,
                query=a.query,
                radius_m=args.radius or a.radius_m,
                address_tokens=list(a.address_tokens),
            )
            for a in THERMAIKOS_AREAS
        ]

    config = SweepConfig(
        areas=areas,
        included_types=list(TYPE_PRESETS.get(args.preset, TYPE_PRESETS["doctors"])),
        text_queries=(args.query or list(DEFAULT_DOCTOR_QUERIES)),
        social_counts_as_none=args.social_counts_as_none,
        booking_counts_as_none=args.booking_counts_as_none,
        max_text_pages=args.max_pages,
        include_closed=args.include_closed,
        include_veterinary=args.include_veterinary,
        max_subdivision_depth=args.max_depth,
        relevance_filter=not args.no_relevance_filter,
        strict_area_filter=not args.no_area_filter,
    )

    client = GooglePlacesClient(api_key, timeout=args.timeout)
    try:
        report = await run_places_sweep(client, config)
    except PlacesApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    if args.csv:
        _write_places_csv(args.csv, report)

    _emit(report.to_dict(), args.pretty)
    return EXIT_OK if report.total else EXIT_NO_RECORDS


def _write_places_csv(path: str, report: Any) -> None:
    """Write every bucket to one CSV, bucket recorded per row."""
    import csv

    columns = [
        "bucket", "relevance", "name", "phone", "address", "website", "website_kind",
        "primary_type", "area", "areas", "distance_m", "medical_signal", "rating",
        "reviews_count", "maps_uri", "place_id",
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for bucket in ("no_website", "borderline", "has_website"):
            for listing in getattr(report, bucket):
                row = listing.to_dict()
                row["bucket"] = bucket
                row["areas"] = ", ".join(row.get("areas") or [])
                writer.writerow(row)


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

    map_cmd = sub.add_parser("map", help="Discover URLs via a site's sitemap(s), no extraction.", parents=[common])
    map_cmd.add_argument("url", help="Site root URL, including scheme.")
    map_cmd.add_argument("--max-urls", type=int, default=500, help="Cap on discovered URLs (default: 500).")
    map_cmd.set_defaults(func=cmd_map)

    submit = sub.add_parser("submit", help="Enqueue a job for the worker cluster.", parents=[common])
    submit.add_argument("url", help="Target URL, including scheme.")
    submit.add_argument("--site", default="universal", help="Extraction strategy identifier.")
    submit.add_argument("--overlay-file", help="JSON file with a declarative extraction overlay.")
    submit.add_argument("--job-id", help="Explicit job ID (default: generated).")
    submit.add_argument("--valkey-url", help="Broker URL (default: $VALKEY_URL).")
    submit.set_defaults(func=cmd_submit)

    places = sub.add_parser(
        "places",
        help="Sweep areas via the Google Places API and split by real web presence.",
        parents=[common],
    )
    places.add_argument("--api-key", help="Google Maps Platform key (default: $GOOGLE_MAPS_API_KEY).")
    places.add_argument("--area", action="append", help="Area to sweep; repeatable. Default: the three Thermaikos localities.")
    places.add_argument("--preset", default="doctors", choices=sorted(("doctors", "medical", "medical_and_vet")), help="Place-type set (default: doctors).")
    places.add_argument("--query", action="append", help="Text query; repeatable. Default: the Greek doctor terms.")
    places.add_argument("--radius", type=float, default=0.0, help="Search radius in metres (default: per-area).")
    places.add_argument("--max-pages", type=int, default=3, help="Text Search pages to follow, 20 results each (default: 3).")
    places.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout in seconds.")
    places.add_argument("--social-counts-as-none", action="store_true", help="Count a Facebook/Instagram-only listing as having no website.")
    places.add_argument("--booking-counts-as-none", action="store_true", help="Count a booking-platform-only listing as having no website.")
    places.add_argument("--include-closed", action="store_true", help="Keep permanently/temporarily closed listings.")
    places.add_argument("--include-veterinary", action="store_true", help="Keep veterinary clinics (excluded by default: a vet is not a doctor).")
    places.add_argument("--max-depth", type=int, default=1, help="Grid subdivision depth when a Nearby page comes back full (default: 1).")
    places.add_argument("--no-relevance-filter", action="store_true", help="Keep listings with no medical signal.")
    places.add_argument("--no-area-filter", action="store_true", help="Keep listings outside every area radius.")
    places.add_argument("--csv", help="Also write all buckets to this CSV path.")
    places.set_defaults(func=cmd_places)

    health = sub.add_parser("health", help="Report dependency status.", parents=[common])
    health.set_defaults(func=cmd_health)

    return parser


def main(argv: list[str] | None = None) -> int:
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
