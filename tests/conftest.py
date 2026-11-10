"""Fixtures for API-level tests."""
import pytest

from helpers import FakeSession, pge_api


@pytest.fixture
def api():
    return pge_api


@pytest.fixture
def session():
    return FakeSession()
