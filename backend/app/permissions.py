from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.tools.registry import Permission, ToolRegistry


@dataclass(frozen=True)
class PermissionDecision:
    tool_name: str
    permission: Permission
    allowed: bool
    reason_code: str


class PermissionEngine:
    """Evaluate the intersection of Skill and runtime Tool permissions."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def evaluate(
        self,
        *,
        tool_name: str,
        manifest_tools: set[str],
        manifest_permission: Permission,
        allow_guarded: bool = False,
    ) -> PermissionDecision:
        definition = self.registry.get(tool_name)
        if definition is None:
            return PermissionDecision(
                tool_name=tool_name,
                permission=Permission.DENY,
                allowed=False,
                reason_code="UNKNOWN_TOOL",
            )
        if tool_name not in manifest_tools:
            return PermissionDecision(
                tool_name=tool_name,
                permission=Permission.DENY,
                allowed=False,
                reason_code="TOOL_NOT_IN_MANIFEST",
            )
        if manifest_permission in {Permission.DENY, Permission.JARVIS_ONLY}:
            return PermissionDecision(
                tool_name=tool_name,
                permission=manifest_permission,
                allowed=False,
                reason_code="MANIFEST_PERMISSION_BLOCKED",
            )
        if definition.permission in {Permission.DENY, Permission.JARVIS_ONLY}:
            return PermissionDecision(
                tool_name=tool_name,
                permission=definition.permission,
                allowed=False,
                reason_code="RUNTIME_PERMISSION_BLOCKED",
            )
        if definition.permission in {Permission.CONFIRM, Permission.GUARDED} and not allow_guarded:
            return PermissionDecision(
                tool_name=tool_name,
                permission=definition.permission,
                allowed=False,
                reason_code="USER_OR_RUNTIME_CONFIRMATION_REQUIRED",
            )
        return PermissionDecision(
            tool_name=tool_name,
            permission=definition.permission,
            allowed=True,
            reason_code="ALLOWED",
        )

    def execute(
        self,
        *,
        tool_name: str,
        payload: dict[str, Any],
        user_id: str,
        manifest_tools: set[str],
        manifest_permission: Permission,
        allow_guarded: bool = False,
    ) -> tuple[PermissionDecision, Any]:
        decision = self.evaluate(
            tool_name=tool_name,
            manifest_tools=manifest_tools,
            manifest_permission=manifest_permission,
            allow_guarded=allow_guarded,
        )
        if not decision.allowed:
            return decision, None
        result = self.registry.execute(
            name=tool_name,
            payload=payload,
            user_id=user_id,
            manifest_tools=manifest_tools,
            manifest_permission=manifest_permission,
            allow_guarded=allow_guarded,
        )
        return decision, result
