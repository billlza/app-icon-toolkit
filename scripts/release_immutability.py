"""Repository-level immutable-release policy validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


MAX_POLICY_JSON_BYTES = 4096


class ReleaseImmutabilityError(RuntimeError):
    """The repository cannot yet guarantee immutable release assets and tags."""


@dataclass(frozen=True)
class ReleaseImmutabilityPolicy:
    enabled: bool
    enforced_by_owner: bool


def parse_release_immutability_policy(payload: str) -> ReleaseImmutabilityPolicy:
    """Parse and require GitHub's exact enabled immutable-release policy."""

    if not isinstance(payload, str):
        raise ReleaseImmutabilityError("immutable-release policy must be text")
    try:
        size = len(payload.encode("utf-8", errors="strict"))
    except UnicodeError as error:
        raise ReleaseImmutabilityError(
            "immutable-release policy is not valid UTF-8"
        ) from error
    if size <= 0 or size > MAX_POLICY_JSON_BYTES:
        raise ReleaseImmutabilityError(
            "immutable-release policy JSON is empty or oversized"
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ReleaseImmutabilityError(
                    f"immutable-release policy repeats JSON key {key!r}"
                )
            value[key] = item
        return value

    def reject_constant(constant: str) -> None:
        raise ReleaseImmutabilityError(
            f"immutable-release policy contains {constant}"
        )

    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except ReleaseImmutabilityError:
        raise
    except json.JSONDecodeError as error:
        raise ReleaseImmutabilityError(
            f"immutable-release policy is invalid JSON: {error.msg}"
        ) from error
    if not isinstance(value, dict) or set(value) != {"enabled", "enforced_by_owner"}:
        raise ReleaseImmutabilityError(
            "immutable-release policy has an unexpected shape"
        )
    enabled = value["enabled"]
    enforced = value["enforced_by_owner"]
    if not isinstance(enabled, bool) or not isinstance(enforced, bool):
        raise ReleaseImmutabilityError(
            "immutable-release policy fields must be booleans"
        )
    if not enabled:
        raise ReleaseImmutabilityError(
            "repository immutable releases must be enabled before finalization"
        )
    return ReleaseImmutabilityPolicy(
        enabled=enabled,
        enforced_by_owner=enforced,
    )
