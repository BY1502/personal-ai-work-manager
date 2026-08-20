from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# These names describe a Skill's intended role. They are deliberately not
# separate model routes: one Backend process owns one configured LLM worker.
LOGICAL_MODEL_PROFILE_NAMES = frozenset(
    {"jarvis-reasoning", "worker-balanced", "worker-fast"}
)


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider_name: str
    model_name: str | None
    timeout_seconds: float


class ModelProfileResolver:
    """Map every logical Skill profile to the one configured LLM worker.

    SKILL.md owns role-specific instructions. Profile names remain in manifests
    and execution logs for compatibility and observability, but they never
    select a second provider or model.
    """

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
            for name in LOGICAL_MODEL_PROFILE_NAMES
        }

    def resolve(self, name: str) -> ModelProfile:
        profile = self._profiles.get(name)
        if profile is None:
            raise ValueError(f"unknown model profile: {name}")
        return profile

    def all(self) -> dict[str, ModelProfile]:
        return dict(self._profiles)
