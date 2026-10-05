"""Domain-neutral role names for closed-world (NOT) rule input contracts.

A closed-world set-difference rule needs to know five things about the
snapshot it treats as complete: which record it is scoped to, which subject
the record is about, which classifier narrows the required set, which field
carries the acceptance status, and where the row came from.  Those are
*roles*, not business field names.

The first production ontology in this platform was a government service
material checklist, so the roles were originally spelled `case_id`,
`service_item_id`, `material_code` and `submit_status`.  A manufacturing or
finance ontology has no `material_code`; forcing one makes the agent stuff
`equipment_id` into a key named after a government form, which then leaks into
the published ontology and into every answer receipt.

This module defines the neutral role names and accepts the original
government-domain spellings as aliases, so existing published projects keep
validating unchanged while new projects can use language that matches their
own domain.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Neutral role -> legacy government-domain alias accepted for compatibility.
SUBMITTED_ROLE_ALIASES: dict[str, str] = {
    "record_id": "case_id",
    "classifier_id": "service_item_id",
    "subject_id": "material_code",
    "status_field": "submit_status",
    "snapshot_version": "snapshot_version",
    "source_locator": "source_locator",
}

REQUIRED_SET_ROLE_ALIASES: dict[str, str] = {
    "classifier_id": "service_item_id",
    "subject_id": "material_code",
    "mandatory": "mandatory",
    "source_locator": "source_locator",
}

SUBMITTED_ROLES: tuple[str, ...] = tuple(SUBMITTED_ROLE_ALIASES)
REQUIRED_SET_ROLES: tuple[str, ...] = tuple(REQUIRED_SET_ROLE_ALIASES)

# The roles whose bound columns must also appear in `key_fields`: the record
# the snapshot is scoped to, and the subject the difference is computed over.
SUBMITTED_KEY_ROLES: tuple[str, ...] = ("record_id", "subject_id")


def _canonical_key_sets(aliases: Mapping[str, str]) -> tuple[frozenset[str], frozenset[str]]:
    return frozenset(aliases), frozenset(aliases.values())


SUBMITTED_KEY_SETS = _canonical_key_sets(SUBMITTED_ROLE_ALIASES)
REQUIRED_SET_KEY_SETS = _canonical_key_sets(REQUIRED_SET_ROLE_ALIASES)


def _resolve(
    bindings: Any,
    aliases: Mapping[str, str],
) -> dict[str, str] | None:
    """Return role -> bound column, or None when the binding set is invalid.

    A valid binding uses exactly the neutral role names, or exactly the legacy
    alias names.  Mixing the two spellings in one object is rejected: it is
    almost always a sign the agent guessed instead of deciding.
    """

    if not isinstance(bindings, dict):
        return None
    present = set(bindings)
    neutral, legacy = _canonical_key_sets(aliases)
    if present == neutral:
        keyed = {role: bindings[role] for role in aliases}
    elif present == legacy:
        reverse = {alias: role for role, alias in aliases.items()}
        keyed = {reverse[alias]: bindings[alias] for alias in legacy}
    else:
        return None
    if any(not str(value).strip() for value in keyed.values()):
        return None
    return {role: str(value).strip() for role, value in keyed.items()}


def resolve_submitted_roles(bindings: Any) -> dict[str, str] | None:
    """Resolve a closed-world snapshot's `field_bindings` into roles."""

    return _resolve(bindings, SUBMITTED_ROLE_ALIASES)


def resolve_required_set_roles(bindings: Any) -> dict[str, str] | None:
    """Resolve a required-set source's `field_bindings` into roles."""

    return _resolve(bindings, REQUIRED_SET_ROLE_ALIASES)


def submitted_roles_are_valid(bindings: Any) -> bool:
    return resolve_submitted_roles(bindings) is not None


def required_set_roles_are_valid(bindings: Any) -> bool:
    return resolve_required_set_roles(bindings) is not None


def key_role_columns(roles: Mapping[str, str]) -> set[str]:
    """The bound columns that must also be declared as `key_fields`."""

    return {str(roles[role]) for role in SUBMITTED_KEY_ROLES if role in roles}


def role_binding_help(aliases: Mapping[str, str] = SUBMITTED_ROLE_ALIASES) -> str:
    """Chinese remedy text naming the accepted role keys."""

    neutral = "、".join(aliases)
    legacy = "、".join(dict.fromkeys(aliases.values()))
    return (
        f"field_bindings 的键必须是角色名（{neutral}），"
        f"值才是本工程真实的列名；也兼容历史政务命名（{legacy}），但不能混用两套拼写。"
    )
