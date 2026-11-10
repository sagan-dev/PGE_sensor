"""Shared helpers. api.py is loaded standalone so these tests run without HA."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = pathlib.Path(__file__).parent / "fixtures"

_spec = importlib.util.spec_from_file_location("pge_api", ROOT / "custom_components" / "pge_sensor" / "api.py")
pge_api = importlib.util.module_from_spec(_spec)
sys.modules["pge_api"] = pge_api
_spec.loader.exec_module(pge_api)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture_text(name))


class FakeResponse:
    def __init__(self, status: int = 200, body: str | dict | list = "", content_type: str = "application/json"):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.headers = {"Content-Type": content_type}

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """Routes ``(METHOD, url-substring)`` to queued responses or exceptions."""

    def __init__(self):
        self.headers: dict = {}
        self.routes: list[tuple[str, str, list]] = []
        self.calls: list[tuple[str, str, dict]] = []

    def add(self, method: str, fragment: str, *responses):
        self.routes.append((method, fragment, list(responses)))
        return self

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url, dict(headers or {})))
        for m, fragment, queue in self.routes:
            if m == method and fragment in url and queue:
                item = queue[0] if len(queue) == 1 else queue.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item
        raise AssertionError(f"unexpected request {method} {url}")

    def count(self, fragment: str) -> int:
        return sum(1 for _, url, _ in self.calls if fragment in url)


@pytest.fixture
def api():
    return pge_api


@pytest.fixture
def session():
    return FakeSession()


def make_client(session, **kwargs):
    return pge_api.PgeScraper("user@example.invalid", "not-a-real-password", session=session, **kwargs)


def ok(name: str) -> FakeResponse:
    return FakeResponse(200, fixture_text(name))
