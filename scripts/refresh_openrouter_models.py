#!/usr/bin/env python3
"""Sync the OpenRouter model catalogue and audit the SSOT's pins against it.

    python scripts/refresh_openrouter_models.py --diff     # fetch, show changes, save
    python scripts/refresh_openrouter_models.py --dry-run  # show changes, save nothing
    python scripts/refresh_openrouter_models.py --rank     # reproduce the VFM ranking
    python scripts/refresh_openrouter_models.py --verify   # check ssot.py pins still exist

Endpoint, catalogue path and ranking weights come from the AI SSOT
(src/infrastructure/ai/ssot.py); nothing about model choice lives in here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.infrastructure.ai.ssot import (  # noqa: E402
    CATALOGUE_PATH,
    ENDPOINTS,
    JOB_PROFILES,
    PRICE_BLEND_IN,
    PRICE_BLEND_OUT,
    AIJob,
    pinned_openrouter_models,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("refresh_openrouter_models")


async def fetch_catalogue() -> list[dict[str, Any]]:
    """Fetch the live model list. Returns [] on failure."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(ENDPOINTS.openrouter_catalogue)
            response.raise_for_status()
            return response.json().get("data", [])
    except Exception as e:  # noqa: BLE001 — a CLI, any failure is just a message
        logger.error(f"Failed to fetch the OpenRouter catalogue: {e}")
        return []


def parse_model(raw: dict[str, Any]) -> dict[str, Any]:
    """Project the catalogue entry onto the fields the SSOT actually reasons about.

    Everything here is copied from the API verbatim. Nothing is inferred: a
    field the catalogue does not publish is stored as None rather than guessed,
    because these values are the evidence behind the model pins.
    """
    architecture = raw.get("architecture") or {}
    pricing = raw.get("pricing") or {}
    reasoning = raw.get("reasoning") or {}
    analysis = (raw.get("benchmarks") or {}).get("artificial_analysis") or {}
    supported = raw.get("supported_parameters") or []

    return {
        "id": raw.get("id"),
        "canonical_slug": raw.get("canonical_slug"),
        "name": raw.get("name"),
        "context_length": raw.get("context_length"),
        "knowledge_cutoff": raw.get("knowledge_cutoff"),
        "pricing": {
            "prompt": pricing.get("prompt"),
            "completion": pricing.get("completion"),
            "input_cache_read": pricing.get("input_cache_read"),
            "input_cache_write": pricing.get("input_cache_write"),
            "internal_reasoning": pricing.get("internal_reasoning"),
        },
        "supported_parameters": supported,
        # Derived strictly from supported_parameters, not guessed.
        "supports_temperature": "temperature" in supported,
        "supports_structured_outputs": "structured_outputs" in supported,
        "supports_tools": "tools" in supported,
        "input_modalities": architecture.get("input_modalities") or [],
        "reasoning": {
            "mandatory": reasoning.get("mandatory"),
            "default_enabled": reasoning.get("default_enabled"),
            "default_effort": reasoning.get("default_effort"),
            "supported_efforts": reasoning.get("supported_efforts"),
        },
        "benchmarks": {
            "intelligence_index": analysis.get("intelligence_index"),
            "coding_index": analysis.get("coding_index"),
            "agentic_index": analysis.get("agentic_index"),
        },
    }


def blended_price(model: dict[str, Any]) -> float:
    """USD per 1M tokens, input-weighted to match the SSOT's ranking formula."""
    pricing = model.get("pricing") or {}
    try:
        prompt = float(pricing.get("prompt") or 0) * 1e6
        completion = float(pricing.get("completion") or 0) * 1e6
    except (TypeError, ValueError):
        return 0.0
    return PRICE_BLEND_IN * prompt + PRICE_BLEND_OUT * completion


def load_snapshot() -> dict[str, Any]:
    if CATALOGUE_PATH.exists():
        try:
            return json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Existing catalogue is unreadable; treating it as empty.")
    return {"models": [], "source": "openrouter", "last_updated": None}


def save_snapshot(payload: dict[str, Any]) -> None:
    CATALOGUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOGUE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.info(f"Wrote {len(payload['models'])} models to {CATALOGUE_PATH}")


def show_diff(current: dict[str, Any], new: dict[str, Any]) -> None:
    """Report added, removed and repriced models between two snapshots."""
    old_by_id = {m["id"]: m for m in current.get("models", [])}
    new_by_id = {m["id"]: m for m in new.get("models", [])}
    added = sorted(new_by_id.keys() - old_by_id.keys())
    removed = sorted(old_by_id.keys() - new_by_id.keys())

    repriced = []
    for mid in sorted(new_by_id.keys() & old_by_id.keys()):
        before, after = blended_price(old_by_id[mid]), blended_price(new_by_id[mid])
        if before and after and abs(after - before) / before > 0.01:
            repriced.append((mid, before, after))

    print(f"\nCatalogue: {len(old_by_id)} -> {len(new_by_id)} models")
    print(f"  added {len(added)}   removed {len(removed)}   repriced {len(repriced)}")

    if added:
        print(f"\nAdded ({len(added)}):")
        for mid in added[:15]:
            m = new_by_id[mid]
            iq = (m.get("benchmarks") or {}).get("intelligence_index")
            iq_text = f"IQ {iq}" if iq is not None else "unbenchmarked"
            print(f"  + {mid:48} {iq_text:16} ${blended_price(m):.3f}/1M")
        if len(added) > 15:
            print(f"    ... and {len(added) - 15} more")

    if removed:
        print(f"\nRemoved ({len(removed)}):")
        for mid in removed[:15]:
            print(f"  - {mid}")
        if len(removed) > 15:
            print(f"    ... and {len(removed) - 15} more")

    if repriced:
        print(f"\nRepriced ({len(repriced)}):")
        for mid, before, after in repriced[:15]:
            direction = "up" if after > before else "down"
            print(f"  ~ {mid:48} ${before:.3f} -> ${after:.3f} ({direction})")
        if len(repriced) > 15:
            print(f"    ... and {len(repriced) - 15} more")


def show_ranking(models: list[dict[str, Any]]) -> None:
    """Reproduce the value-for-money table behind the SSOT's model pins."""
    print("\nvalue = intelligence_index / blended $ per 1M")
    print(f"blended = {PRICE_BLEND_IN} * prompt + {PRICE_BLEND_OUT} * completion\n")

    for job, profile in JOB_PROFILES.items():
        if job is AIJob.EMBED:
            continue  # Gemini-served; not in this catalogue
        rows = []
        for m in models:
            mid = m["id"]
            if mid.startswith("~") or ":batch" in mid:
                continue
            iq = (m.get("benchmarks") or {}).get("intelligence_index")
            if iq is None:
                continue
            if (m.get("context_length") or 0) < profile.model.context_length * 0.15:
                continue
            if profile.expects_json and not m.get("supports_structured_outputs"):
                continue
            price = blended_price(m)
            if price <= 0:
                continue
            rows.append((iq / price, iq, price, mid, m.get("supports_temperature")))
        rows.sort(key=lambda r: -r[0])

        chain = " -> ".join(profile.model_chain)
        print(f"=== {job.value.upper()} ===  pinned: {chain}")
        print(f"{'model':46} {'IQ':>5} {'$/1M':>8} {'temp':>5} {'value':>8}")
        for score, iq, price, mid, temp in rows[:8]:
            marker = " *" if mid in profile.model_chain else "  "
            print(f"{marker}{mid[:44]:44} {iq:>5.1f} {price:>8.3f} {str(temp):>5} {score:>8.1f}")
        print()


def verify_pins(models: list[dict[str, Any]]) -> int:
    """Check every SSOT pin still exists and still matches its recorded evidence.

    Returns a process exit code: non-zero when a pin is missing, since that
    means the SSOT names a model the catalogue can no longer serve.
    """
    by_id = {m["id"]: m for m in models}
    missing: list[str] = []
    drifted: list[str] = []

    print("\nVerifying SSOT pins against the live catalogue:\n")
    for model_id in pinned_openrouter_models():
        live = by_id.get(model_id)
        if live is None:
            print(f"  MISSING   {model_id}")
            missing.append(model_id)
            continue

        notes = []
        if not live.get("supports_temperature"):
            notes.append("no temperature support")
        reasoning = live.get("reasoning") or {}
        if reasoning.get("mandatory"):
            notes.append(f"reasoning mandatory (default {reasoning.get('default_effort')})")
        iq = (live.get("benchmarks") or {}).get("intelligence_index")
        detail = f"IQ {iq}" if iq is not None else "unbenchmarked"
        suffix = f"  [{'; '.join(notes)}]" if notes else ""
        print(f"  ok        {model_id:44} {detail:18} ${blended_price(live):.3f}/1M{suffix}")
        if notes:
            drifted.append(model_id)

    print()
    if missing:
        print(f"{len(missing)} pinned model(s) no longer in the catalogue. "
              f"Update src/infrastructure/ai/ssot.py.")
        return 1
    print(f"All {len(pinned_openrouter_models())} pinned models present.")
    if drifted:
        print("Models carrying caveats are annotated above; confirm ssot.py still records them.")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diff", action="store_true", help="show catalogue changes before saving")
    parser.add_argument("--dry-run", action="store_true", help="show changes but save nothing")
    parser.add_argument("--rank", action="store_true", help="reproduce the value-for-money ranking")
    parser.add_argument("--verify", action="store_true", help="check the SSOT's pins still resolve")
    args = parser.parse_args()

    logger.info("Fetching the OpenRouter catalogue...")
    raw_models = await fetch_catalogue()
    if not raw_models:
        return 1

    parsed = [parse_model(m) for m in raw_models]
    snapshot = {
        "models": parsed,
        "source": "openrouter",
        "endpoint": ENDPOINTS.openrouter_catalogue,
        "last_updated": datetime.now(UTC).isoformat(),
        "note": "Auto-generated by scripts/refresh_openrouter_models.py. Do not hand-edit.",
    }

    if args.diff or args.dry_run:
        show_diff(load_snapshot(), snapshot)
    if args.rank:
        show_ranking(parsed)

    exit_code = verify_pins(parsed) if args.verify else 0

    # --rank and --verify are read-only audits; only a plain run or --diff writes.
    if not args.dry_run and not args.rank and not args.verify:
        save_snapshot(snapshot)
    elif args.dry_run:
        logger.info("Dry run: catalogue not written.")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
