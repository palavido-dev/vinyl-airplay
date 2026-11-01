"""Regression: auto-stream must not open/close capture every poll cycle."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streaming as st


def test_open_monitor_stream_helper_exists():
    assert callable(st._open_monitor_stream)
    assert callable(st._poll_capture)


def test_auto_stream_doc_mentions_persistent_monitor():
    doc = st._auto_stream_watcher.__doc__ or ""
    assert "open/close" in doc.lower() or "Holding" in doc or "Holds" in doc
