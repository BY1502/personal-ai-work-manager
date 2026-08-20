from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def legacy_api_auth_compatibility(monkeypatch: pytest.MonkeyPatch):
    """Old Phase 1/2 tests predate login and exercise domain behavior directly.

    Authentication-specific tests pass ``auth_required=True`` to ``create_app``.
    Production keeps the secure default because AUTH_REQUIRED defaults to true.
    """

    monkeypatch.setenv("AUTH_REQUIRED", "false")
