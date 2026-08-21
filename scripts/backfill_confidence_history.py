"""Regrade persisted analysis history with the canonical signal-score rules."""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

init_db = importlib.import_module("app.db.session").init_db
backfill_confidence_history = importlib.import_module(
    "app.services.confidence_history"
).backfill_confidence_history


if __name__ == "__main__":
    init_db()
    print(f"updated={backfill_confidence_history()}")
