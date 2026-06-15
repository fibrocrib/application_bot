"""CV loading + caching."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pdfplumber


@lru_cache(maxsize=1)
def load_text(cv_path: str) -> str:
    p = Path(cv_path)
    if not p.exists():
        raise FileNotFoundError(f"CV not found at {cv_path}")
    with pdfplumber.open(p) as pdf:
        return "\n\n".join((page.extract_text() or "") for page in pdf.pages).strip()
