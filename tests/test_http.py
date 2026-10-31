import hashlib

import pytest
import requests

from bus_sync.http import FetchError, fetch


class Response:
    def __init__(self, body=b"complete", headers=None, status=200, error=False):
        self.body, self.status_code, self.error = body, status, error
        self.headers = headers or {"Content-Length": str(len(body))}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, size):
        yield self.body[:2]
        if self.error:
            raise requests.ConnectionError("stream interrupted")
        yield self.body[2:]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def fake_session(monkeypatch, responses):
    calls = []
    class Session:
        def mount(self, *args):
            pass
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return responses.pop(0)
        def close(self):
            pass
    monkeypatch.setattr(requests, "Session", Session)
    return calls


def test_complete_download_hash_and_atomic_replace(monkeypatch, tmp_path):
    fake_session(monkeypatch, [Response()])
    dest = tmp_path / "file"
    dest.write_bytes(b"old")
    metadata = fetch("https://download.geofabrik.de/example", dest)
    assert dest.read_bytes() == b"complete"
    assert metadata["sha256"] == hashlib.sha256(b"complete").hexdigest()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["file"]


@pytest.mark.parametrize("response", [Response(b""), Response(headers={"Content-Length": "999"}),
                                       Response(error=True), Response(status=500)])
def test_bad_response_never_overwrites_valid_file(monkeypatch, tmp_path, response):
    fake_session(monkeypatch, [response])
    dest = tmp_path / "file"
    dest.write_bytes(b"old")
    with pytest.raises(FetchError):
        fetch("https://download.geofabrik.de/example", dest)
    assert dest.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["file"]


def test_untrusted_redirect_is_not_followed(monkeypatch, tmp_path):
    calls = fake_session(monkeypatch, [Response(status=302, headers={"Location": "http://127.0.0.1/private"})])
    with pytest.raises(FetchError, match="Unexpected"):
        fetch("https://download.geofabrik.de/example", tmp_path / "file")
    assert len(calls) == 1


def test_no_osm_write_endpoint_or_generic_post(monkeypatch, tmp_path):
    calls = fake_session(monkeypatch, [])
    with pytest.raises(FetchError):
        fetch("https://api.openstreetmap.org/api/0.6/changeset/create", tmp_path / "file", method="PUT")
    with pytest.raises(FetchError, match="POST is restricted"):
        fetch("https://www.gbis.go.kr/anything", tmp_path / "file", method="POST")
    assert not calls

