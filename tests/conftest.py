"""Shared test fixtures."""

import pytest

from nevoflux_muse.testing.account import AccountDouble

DEAD_URL = "http://127.0.0.1:9"


@pytest.fixture
def account(monkeypatch):
    double = AccountDouble().start()
    monkeypatch.setenv("NEVOFLUX_ACCOUNT_URL", double.url)
    yield double
    double.stop()
