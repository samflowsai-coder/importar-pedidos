"""Smoke tests for app.pipeline.process.

Validates the orchestrator's branching without hitting the LLM:
- Unknown format → returns None (no parser invoked).
- Sample real PDF → produces an Order with at least one item.

The second test exercises the full deterministic pipeline (classifier →
extractor → cascade of parsers → normalize → validate). It does NOT exercise
the LLM fallback (would require OPENROUTER_API_KEY).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.ingestion.file_loader import LoadedFile
from app.pipeline import process

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def _load(filename: str) -> LoadedFile:
    path = SAMPLES / filename
    return LoadedFile(path=path, extension=path.suffix.lower(), raw=path.read_bytes())


def test_unknown_format_returns_none(tmp_path: Path) -> None:
    weird = tmp_path / "ignore.txt"
    weird.write_text("not a pedido")
    file = LoadedFile(path=weird, extension=".txt", raw=weird.read_bytes())
    assert process(file) is None


@pytest.mark.parametrize("sample_name", [
    "PEDIDO BEIRA RIO.pdf",
    "PEDIDO CENTAURO.pdf",
    "PEDIDO KOLOSH.pdf",
])
def test_real_sample_yields_order_with_items(sample_name: str) -> None:
    sample_path = SAMPLES / sample_name
    if not sample_path.exists():
        pytest.skip(f"sample missing: {sample_name}")

    order = process(_load(sample_name))

    assert order is not None, f"pipeline returned None for {sample_name}"
    assert len(order.items) > 0, "expected at least one parsed item"
    # source_file is set by the orchestrator
    assert order.source_file.endswith(sample_name)
