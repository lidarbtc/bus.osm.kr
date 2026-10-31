"""Preservation rules. Expiry never turns an exception into a deletion."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit


def load_exceptions(path: Path, today: date | None = None) -> dict:
    today = today or date.today()
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("Unsupported exception schema_version")
    defaults = document.get("defaults", {})
    if (defaults.get("unmatched_osm_action") != "preserve"
            or defaults.get("expired_exception_action") != "preserve_and_review"):
        raise ValueError("Exception defaults must preserve unmatched and expired objects")
    entries = document.get("exceptions")
    if not isinstance(entries, list):
        raise ValueError("exceptions must be a list")
    seen_ids, seen_objects = set(), set()
    for entry in entries:
        for field in ("id", "classification", "reason", "scope", "reviewer"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f"Missing exception field: {field}")
        if entry["id"] in seen_ids:
            raise ValueError(f"Duplicate exception id: {entry['id']}")
        seen_ids.add(entry["id"])
        if entry.get("action") != "preserve":
            raise ValueError("Only preserve exception actions are supported")
        if entry["classification"] not in {"valid_outside_source_scope", "needs_review"}:
            raise ValueError("Unsupported exception classification")
        refs = entry.get("osm_objects")
        if not isinstance(refs, list) or not refs:
            raise ValueError("An exception needs at least one OSM object")
        for ref in refs:
            if not isinstance(ref, str) or not re.fullmatch(r"(?:node|way|relation)/[1-9][0-9]*", ref):
                raise ValueError(f"Invalid OSM object reference: {ref}")
            if ref in seen_objects:
                raise ValueError(f"Duplicate exception object: {ref}")
            seen_objects.add(ref)
        evidence = entry.get("evidence_urls")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("An exception requires evidence_urls")
        if any(not isinstance(u, str) or urlsplit(u).scheme not in {"http", "https"}
               or not urlsplit(u).hostname for u in evidence):
            raise ValueError("Invalid exception evidence URL")
        try:
            verified = date.fromisoformat(entry["verified_at"])
            review_after = date.fromisoformat(entry["review_after"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid exception verification/review date") from exc
        if review_after < verified:
            raise ValueError("review_after must not precede verified_at")
        entry["review_due"] = today >= review_after
    return document
