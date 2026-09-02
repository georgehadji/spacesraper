# Contract tests for the headless CLI.
# External agents depend on three things: JSON alone on stdout, stable exit
# codes, and diagnostics kept on stderr. Each is asserted here.

import json
import subprocess
import sys
from pathlib import Path

import pytest

import cli

ROOT = Path(__file__).resolve().parent.parent

RECORD_HTML = """
<html><body>
  <script type="application/ld+json">
  {"@context":"https://schema.org","@type":"Product","name":"CLI Test Widget"}
  </script>
</body></html>
"""

EMPTY_HTML = "<html><body><p>nothing structured here</p></body></html>"


def run_cli(*args, stdin: str = None):
    return subprocess.run(
        [sys.executable, "cli.py", *args],
        cwd=ROOT, input=stdin, capture_output=True, text=True, timeout=180,
    )


def test_extract_from_file_emits_records(tmp_path):
    page = tmp_path / "page.html"
    page.write_text(RECORD_HTML, encoding="utf-8")

    result = run_cli("extract", "--html-file", str(page), "--url", "https://example.com/p")

    assert result.returncode == cli.EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    assert payload["record_count"] == 1
    assert payload["records"][0]["data"]["name"] == "CLI Test Widget"


def test_extract_reads_stdin():
    result = run_cli("extract", "--url", "https://example.com/p", stdin=RECORD_HTML)

    assert result.returncode == cli.EXIT_OK, result.stderr
    assert json.loads(result.stdout)["record_count"] == 1


def test_stdout_is_json_only_even_with_verbose_logging(tmp_path):
    page = tmp_path / "page.html"
    page.write_text(RECORD_HTML, encoding="utf-8")

    result = run_cli("extract", "--html-file", str(page), "--verbose")

    # Parsing the whole of stdout proves no log line leaked into it.
    assert json.loads(result.stdout)["record_count"] == 1


def test_no_records_exits_one(tmp_path):
    page = tmp_path / "empty.html"
    page.write_text(EMPTY_HTML, encoding="utf-8")

    result = run_cli("extract", "--html-file", str(page))

    assert result.returncode == cli.EXIT_NO_RECORDS
    assert json.loads(result.stdout)["record_count"] == 0


def test_missing_input_file_exits_usage():
    result = run_cli("extract", "--html-file", "does-not-exist.html")

    assert result.returncode == cli.EXIT_USAGE
    assert result.stdout == ""


def test_scrape_rejects_internal_target():
    result = run_cli("scrape", "http://127.0.0.1:8000/admin")

    assert result.returncode == cli.EXIT_USAGE
    assert result.stdout == ""


def test_scrape_rejects_unresolvable_host():
    # .invalid is reserved by RFC 2606; the SSRF guard rejects it before any fetch.
    result = run_cli("scrape", "https://spacescraper.invalid/page", "--timeout", "5")

    assert result.returncode == cli.EXIT_USAGE
    assert result.stdout == ""


@pytest.mark.asyncio
async def test_scrape_fetch_failure_exits_three(monkeypatch, capsys):
    """A transport error must produce a JSON error document and exit code 3."""
    async def boom(url, timeout):
        raise ConnectionError("connection reset by peer")

    monkeypatch.setattr(cli, "_fetch_http", boom)
    monkeypatch.setattr(cli, "validate_outbound_url", lambda url, **kw: None, raising=False)
    monkeypatch.setattr(
        "src.security.ssrf_guard.validate_outbound_url", lambda url, **kw: None
    )

    args = cli.build_parser().parse_args(["scrape", "https://example.com/page"])
    exit_code = await cli.cmd_scrape(args)

    assert exit_code == cli.EXIT_FAILURE
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert "connection reset by peer" in payload["error"]


def test_health_reports_required_and_optional_checks():
    result = run_cli("health")

    payload = json.loads(result.stdout)
    assert payload["checks"]["extraction"]["ok"] is True
    # The broker and browser are optional, so health stays green without them.
    assert result.returncode == cli.EXIT_OK
    assert set(payload["checks"]) == {"extraction", "broker", "browser"}


def test_shared_flags_accepted_before_and_after_subcommand(tmp_path):
    page = tmp_path / "page.html"
    page.write_text(RECORD_HTML, encoding="utf-8")

    after = run_cli("extract", "--html-file", str(page), "--pretty")
    before = run_cli("--pretty", "extract", "--html-file", str(page))

    assert after.returncode == cli.EXIT_OK
    assert before.returncode == cli.EXIT_OK
    assert "\n  " in after.stdout, "expected indented JSON"
    assert json.loads(before.stdout)["record_count"] == json.loads(after.stdout)["record_count"]


@pytest.mark.parametrize("command", ["extract", "scrape", "submit", "health"])
def test_every_subcommand_has_help(command):
    result = run_cli(command, "--help")
    assert result.returncode == 0
    assert command in result.stdout
