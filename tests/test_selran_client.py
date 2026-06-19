"""GL-R1-010: assert the orchestrator client's two contract MUSTs.

_req is the single chokepoint that (1) attaches the x-selran-token badge and (2) targets
loopback. Nothing tested it — a dropped badge or off-loopback base would pass silently.
Also covers HTTPError -> SelranError mapping.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from selran_canvas import _selran_client as c


class _FakeResp:
    def __init__(self, payload: bytes) -> None:
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._p


def test_req_attaches_badge_and_targets_loopback(monkeypatch):
    monkeypatch.setenv("SELRAN_APP_TOKEN", "test-badge-123")
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeResp(json.dumps({"url": "postgres://x"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert c.db_url("canvas") == "postgres://x"
    req = captured["req"]
    assert req.get_header("X-selran-token") == "test-badge-123"  # badge attached
    assert req.full_url.startswith("http://127.0.0.1")           # loopback only


def test_req_maps_httperror_to_selranerror(monkeypatch):
    monkeypatch.setenv("SELRAN_APP_TOKEN", "t")

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b"err"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(c.SelranError):
        c.db_url("canvas")
