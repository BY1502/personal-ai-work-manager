from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider_name: str
    model_name: str | None
    timeout_seconds: float


class ModelProfileResolver:
    """Maps stable Skill profile names to the configured Worker adapter."""

    def __init__(self, worker: Any) -> None:
        provider_name = getattr(worker, "provider_name", "unknown")
        model_name = getattr(worker, "model_name", None)
        self._profiles = {
            name: ModelProfile(
                name=name,
                provider_name=provider_name,
                model_name=model_name,
                timeout_seconds=30.0,
            )
            for name in ("jarvis-reasoning", "worker-balanced", "worker-fast")
        }

    def resolve(self, name: str) -> ModelProfile:
        profile = self._profiles.get(name)
        if profile is None:
            raise ValueError(f"unknown model profile: {name}")
        return profile

    def all(self) -> dict[str, ModelProfile]:
        return dict(self._profiles)
