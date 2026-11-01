"""Downloads from public data providers; deliberately no general write API."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PUBLIC_HOSTS = frozenset({
    "data.seoul.go.kr", "datafile.seoul.go.kr", "www.gbis.go.kr",
    "openapi.gbis.go.kr", "download.geofabrik.de",
})
USER_AGENT = "osm-korea-bus-sync/0.1 (read-only public transit comparison)"


class FetchError(RuntimeError):
    pass


class EmptyDownload(FetchError):
    """The provider answered with an empty file, typically one it announced but has not filled yet."""


def fetch(url: str, destination: Path, method: str = "GET", data=None,
          allowed_hosts=None) -> dict:
    """Atomically save a complete response, retaining an existing file on failure.

    POST is only allowed for Seoul's public file download form. URLs returned by
    providers and redirect targets are checked before making each request.
    """
    hosts = set(allowed_hosts) if allowed_hosts is not None else PUBLIC_HOSTS
    method = method.upper()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    original_url = url
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=frozenset({"GET", "HEAD", "POST"}), respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    temporary = None
    try:
        for _ in range(6):
            parts = urlsplit(url)
            if (parts.scheme not in {"http", "https"} or parts.hostname not in hosts
                    or parts.username or parts.password or parts.port not in {None, 80, 443}):
                raise FetchError(f"Unexpected download URL: {url}")
            if method not in {"GET", "POST"}:
                raise FetchError(f"Unsupported download method: {method}")
            if method == "POST" and (parts.hostname != "datafile.seoul.go.kr"
                    or parts.path != "/bigfile/iot/inf/nio_download.do"):
                raise FetchError("POST is restricted to the public Seoul file download form")
            response = session.request(method, url, data=data, stream=True,
                                       allow_redirects=False, timeout=(20, 120),
                                       headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
            if response.status_code in {301, 302, 303, 307, 308}:
                target = response.headers.get("Location")
                response.close()
                if not target:
                    raise FetchError("Redirect without Location")
                url = urljoin(url, target)
                if response.status_code == 303:
                    method, data = "GET", None
                continue
            response.raise_for_status()
            digest = hashlib.sha256()
            size = 0
            with response, tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as fh:
                temporary = Path(fh.name)
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        size += len(chunk)
                        if size > 2 * 1024**3:
                            raise FetchError("Download exceeded the 2 GiB limit")
                        digest.update(chunk)
                        fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            expected = response.headers.get("Content-Length")
            if not size:
                raise EmptyDownload(f"Empty download from {original_url}")
            if expected and not response.headers.get("Content-Encoding") and size != int(expected):
                raise FetchError(f"Incomplete download from {original_url}: {size} bytes (expected {expected})")
            temporary.replace(destination)
            temporary = None
            return {"url": original_url, "resolved_url": url, "sha256": digest.hexdigest(),
                    "bytes": size, "last_modified": response.headers.get("Last-Modified"),
                    "content_type": response.headers.get("Content-Type"),
                    "etag": response.headers.get("ETag")}
        raise FetchError("Too many download redirects")
    except (requests.RequestException, OSError, ValueError) as exc:
        raise FetchError(f"Download failed for {original_url}: {exc}") from exc
    finally:
        session.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)

