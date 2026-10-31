"""Fetch one dated, checksum-verified national extract from Geofabrik."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .http import fetch

LANDING = "https://download.geofabrik.de/asia/south-korea.html"
ROOT = "https://download.geofabrik.de/asia/"


def checksum(path: Path, algorithm="sha256") -> str:
    digest = hashlib.new(algorithm, usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_osm(data_dir: Path) -> tuple[Path, dict]:
    raw = Path(data_dir) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".osm-", dir=raw) as name:
        temporary = Path(name)
        fetch(LANDING, temporary / "index.html")
        page = (temporary / "index.html").read_text(encoding="utf-8")
        names = set(re.findall(r'href=["\'](south-korea-\d{6}\.osm\.pbf)["\']', page))
        if not names:
            raise ValueError("No dated national OSM extract found on the Geofabrik page")
        filename = max(names, key=lambda n: datetime.strptime(n[12:18], "%y%m%d"))
        # Dated URL + sidecar avoids racing a changing 'latest' alias.
        sidecar_url = ROOT + filename + ".md5"
        fetch(sidecar_url, temporary / "checksum.md5")
        sidecar = (temporary / "checksum.md5").read_text().strip()
        match = re.fullmatch(r"([0-9a-fA-F]{32})\s+\*?" + re.escape(filename), sidecar)
        if not match:
            raise ValueError("Invalid Geofabrik MD5 sidecar")
        expected = match[1].lower()
        path = raw / filename
        if not path.is_file() or checksum(path, "md5") != expected:
            fetch(ROOT + filename, temporary / filename)
            if checksum(temporary / filename, "md5") != expected:
                raise ValueError("Geofabrik download checksum mismatch")
            (temporary / filename).replace(path)
        manifest = {"url": ROOT + filename, "landing_url": LANDING,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "sha256": checksum(path), "md5": expected, "bytes": path.stat().st_size,
                    "coverage": "national_extract", "attribution": "© OpenStreetMap contributors",
                    "license": "https://www.openstreetmap.org/copyright"}
        (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (temporary / "manifest.json").replace(path.with_suffix(path.suffix + ".json"))
    return path, manifest

