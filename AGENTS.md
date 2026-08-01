# Repository Guidelines

## Project Structure & Module Organization
`main.py` is the FastAPI entrypoint. Core application code lives under `src/`, with domain models in `src/domain/`, orchestration in `src/application/`, adapters in `src/infrastructure/`, and extraction logic in `src/extractors/`. Tests live in `tests/` and `tests/integration/`. Configuration files are in `config/`, `pipeline_config.yaml`, and `sources.yaml`. Utility scripts sit in `scripts/`.

## Build, Test, and Development Commands
- `python main.py`: start the API locally.
- `python worker_scraper.py`, `python worker_processor.py`, `python worker_reporter.py`: run the worker processes.
- `python -m pytest`: run the full test suite.
- `python -m py_compile main.py src/**/*.py worker_*.py`: quick syntax check before committing.
- `docker-compose up -d --build`: start the containerized stack with Redis and workers.

## Running Headlessly (external agents)
`cli.py` is the non-interactive entrypoint. It needs no broker, no worker
processes, and no API: one process in, one JSON document out on stdout, all logs
on stderr.

- `python cli.py health`: dependency report. Only `extraction` is required; the
  broker and browser are optional and degrade gracefully.
- `python cli.py scrape <url> [--browser] [--timeout N] [--pretty]`: fetch and
  extract. HTTP by default; `--browser` uses headless Chromium for JS pages.
- `python cli.py extract --html-file <path> --url <url>`: offline extraction from
  local HTML, also accepted on stdin. Fully deterministic — use it in tests.
- `python cli.py submit <url>`: enqueue for the cluster. Fails loudly rather than
  writing to a private in-memory queue no worker can read.
- `docker compose run --rm cli scrape <url> --pretty`: the same CLI in the image.

Exit codes are part of the contract: `0` success, `1` ran but found no records,
`2` usage or input error, `3` fetch or backend failure. Never print anything but
JSON to stdout from these commands.

Long-running alternatives: `python boot.py` (all four processes),
`start_all.bat` on Windows, `docker compose up -d` (full cluster), or
`python spacescraper.py` (single-process tower). The HTTP API is the other
agent-facing surface: register a key at `POST /auth/register`, then
`POST /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/records`.

Offline note: without a reachable broker every queue client falls back to a
*private* in-memory store, so separate processes cannot exchange jobs offline.
Use `cli.py` for single-process work, or run Redis for the cluster.

## Coding Style & Naming Conventions
Use Python 3.11+ with 4-space indentation, type hints, and ASCII-only edits unless the file already uses Unicode. Prefer `snake_case` for functions, variables, and module names; `PascalCase` for classes and Pydantic models. Keep docstrings short and factual. Follow the existing pattern of small, composable services instead of large monoliths.

## Testing Guidelines
Use `pytest` and `pytest-asyncio` for unit and integration tests. Name tests `test_*.py`; place broader flow checks under `tests/integration/`. When changing pipeline, queue, or storage behavior, add a regression test that exercises the affected path. Run the targeted test module first, then the full suite if the change is broad.

## Commit & Pull Request Guidelines
Use short imperative commit messages with a prefix when useful, such as `chore: tidy repository scope` or `fix: handle queue retry`. Pull requests should summarize the change, list verification steps, and call out any behavioral impact or removed surface area. Include screenshots only for UI work.

## Security & Configuration Tips
Keep secrets in `.env`; do not commit API keys, database URLs, or webhook tokens. Review `src/security/` and `src/auth_middleware.py` when changing request handling, outbound URLs, or rate limiting. Treat generated databases and logs in the repo root as disposable artifacts.
