"""Shared pytest setup. Area-specific fixtures live in tests/<area>/conftest.py."""

from __future__ import annotations

import os

import pytest

# Keep any accidental app bootstrap away from real data and real networks.
os.environ.setdefault("SCHEMA_DATA_DIR", os.path.join(os.path.dirname(__file__), ".pytest-data"))
os.environ.setdefault("SCHEMA_DEMO", "0")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: needs real sibling modules; run during integration"
    )
    config.addinivalue_line("markers", "real_scrypt: hash passwords at the production scrypt cost")


@pytest.fixture(autouse=True)
def _cheap_password_hashing(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Production scrypt (N=2**17, ~0.4 s a hash) would make every signed-in test env that much slower;
    tests that check the real cost are marked ``real_scrypt``."""
    if request.node.get_closest_marker("real_scrypt"):
        return
    from schema_app import auth

    monkeypatch.setattr(auth, "_SCRYPT_N", 2**14)
