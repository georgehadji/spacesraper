# Guards against module-level breakage: missing imports, reserved SQLAlchemy
# attribute names, optional dependencies imported unconditionally. Several such
# bugs reached the repo because no test ever imported the affected module.

import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Vendored third-party trees and tooling output are not ours to import.
SKIP_DIRS = {
    "tests", "scripts", "extracted_scrapers", "graphify-out", ".venv", "venv", ".git",
    "google-maps-scraper-main", ".reasonix", ".agents", ".claude", "logs",
    "Deep-Research-With-Web-Scraping-by-LLM-And-AI-Agent-main",
}


def _project_modules():
    names = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        parts = rel.parts[:-1] if rel.name == "__init__.py" else rel.parts[:-1] + (rel.stem,)
        if parts:
            names.append(".".join(parts))
    return names


@pytest.mark.parametrize("module_name", _project_modules())
def test_module_imports_cleanly(module_name):
    importlib.import_module(module_name)
