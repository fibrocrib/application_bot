"""Persistent record of every (company, role) we've already touched, so the
daily run never re-applies. Stored as JSON committed back to the repo."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

STATE_FILE = Path("state.json")
_lock = Lock()


@dataclass
class Record:
    key: str  # f"{company}::{role}"
    company: str
    role: str
    status: str  # applied | skipped | failed
    reason: str
    role_url: str
    fit_score: float
    timestamp: str


def load() -> dict[str, Record]:
    if not STATE_FILE.exists():
        return {}
    raw = json.loads(STATE_FILE.read_text() or "{}")
    return {k: Record(**v) for k, v in raw.items()}


def save(records: dict[str, Record]) -> None:
    with _lock:
        STATE_FILE.write_text(
            json.dumps({k: v.__dict__ for k, v in records.items()},
                       indent=2, sort_keys=True)
        )


def make_key(company: str, role: str) -> str:
    return f"{company.strip().lower()}::{role.strip().lower()}"


def already_seen(records: dict[str, Record], company: str, role: str) -> bool:
    return make_key(company, role) in records
