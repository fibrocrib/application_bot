"""Shared dataclasses used across discovery, matching, and application."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class JobLead:
    title: str
    company: str
    location: str
    description: str
    source: str  # which discovery provider surfaced it
    source_url: str  # where we found it (for logs only, not for applying)

    def dedupe_key(self) -> tuple[str, str]:
        return (self.company.strip().lower(), self.title.strip().lower())


@dataclass
class ResolvedJob:
    """A JobLead plus the company's careers URL and the matching role on it."""
    lead: JobLead
    careers_url: str
    role_url: str  # URL on the company portal we'll actually apply through
    full_description: str = ""


@dataclass
class ApplicationResult:
    lead: JobLead
    status: str  # applied | skipped | failed
    reason: str = ""
    role_url: str = ""
    fit_score: float = 0.0
    statement: str = ""
    artefacts: list[str] = field(default_factory=list)  # screenshot paths
    dry_run: bool = False
