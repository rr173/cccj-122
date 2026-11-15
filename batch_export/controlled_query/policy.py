"""Authorization policy: registered identities, their groups, and grants.

The policy file is a JSON document administered next to the relation
catalog::

    {
      "version": 1,
      "identities": [
        {"name": "alice", "groups": ["reporting", "team-a"]},
        {"name": "bob",   "groups": []}
      ],
      "grants": [
        {"subject": "alice",   "scheme": "...", "identifier": "...",
         "effect": "full"},
        {"subject": "team-a",  "scheme": "...", "identifier": "...",
         "effect": "summary"},
        {"subject": "reporting", "scheme": "...", "identifier": "...",
         "effect": "deny"}
      ]
    }

A grant subject is either a registered identity or a group that at least one
registered identity belongs to; identity names and group names must be
disjoint so a grant can never be ambiguous about what it names.

Resolution for one identity and one entity ``(scheme, identifier)`` collects
every grant whose subject is the identity itself or one of its groups:

* any ``deny``            -> the entity is invisible;
* no ``full``/``summary`` -> the entity is invisible;
* any ``summary``         -> ``summary``;
* only ``full`` grants    -> ``full``.

The outcome is order-independent.  A registered identity with no applicable
grants simply sees empty relations; an *unregistered* identity is an
invocation error.  Entity identities are matched as exact ``(scheme,
identifier)`` pairs -- never as concatenated strings, so different schemes
with the same identifier can never collide.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import ConfigError, InvocationError

POLICY_VERSION = 1

EFFECT_FULL = "full"
EFFECT_SUMMARY = "summary"
EFFECT_DENY = "deny"
_EFFECTS = (EFFECT_FULL, EFFECT_SUMMARY, EFFECT_DENY)


@dataclass(frozen=True)
class Identity:
    name: str
    groups: tuple


@dataclass(frozen=True)
class Grant:
    subject: str
    scheme: str
    identifier: str
    effect: str


class Policy:
    def __init__(self, identities: dict, grants: tuple):
        self.identities = identities  # name -> Identity
        self.grants = grants

    def identity(self, name: str) -> Identity:
        ident = self.identities.get(name)
        if ident is None:
            raise InvocationError(
                f"unknown identity {name!r}: not registered in the policy file"
            )
        return ident

    def resolve(self, identity: Identity) -> dict:
        """Map each visible entity ``(scheme, identifier)`` to ``full`` or
        ``summary`` for this identity; entities absent from the map are
        invisible."""
        subjects = {identity.name, *identity.groups}
        effects: dict = {}
        for grant in self.grants:
            if grant.subject not in subjects:
                continue
            key = (grant.scheme, grant.identifier)
            current = effects.get(key)
            if grant.effect == EFFECT_DENY:
                effects[key] = EFFECT_DENY
            elif grant.effect == EFFECT_SUMMARY:
                if current != EFFECT_DENY:
                    effects[key] = EFFECT_SUMMARY
            else:  # full
                if current is None:
                    effects[key] = EFFECT_FULL
        return {key: effect for key, effect in effects.items()
                if effect != EFFECT_DENY}


def load_policy(path: str) -> Policy:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise InvocationError(f"cannot read policy file {path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise InvocationError(f"policy file {path} is not valid JSON: {exc}") from None
    except UnicodeDecodeError as exc:
        raise InvocationError(f"policy file {path} is not valid UTF-8: {exc}") from None
    return parse_policy(document)


def parse_policy(document) -> Policy:
    where = "policy"
    if not isinstance(document, dict):
        raise ConfigError(f"{where}: top level must be a JSON object")
    version = document.get("version")
    if version != POLICY_VERSION:
        raise ConfigError(
            f"{where}: unsupported version {version!r}; expected {POLICY_VERSION}"
        )
    raw_identities = document.get("identities")
    if not isinstance(raw_identities, list) or not raw_identities:
        raise ConfigError(f"{where}: 'identities' must be a non-empty array")
    identities: dict = {}
    group_names = set()
    for index, item in enumerate(raw_identities, start=1):
        iwhere = f"{where} identity #{index}"
        if not isinstance(item, dict):
            raise ConfigError(f"{iwhere}: must be an object")
        name = _required_text(item, "name", iwhere)
        if name in identities:
            raise ConfigError(f"{where}: duplicate identity {name!r}")
        raw_groups = item.get("groups", [])
        if not isinstance(raw_groups, list):
            raise ConfigError(f"{iwhere}: 'groups' must be an array")
        groups = []
        for group in raw_groups:
            if not isinstance(group, str) or not group:
                raise ConfigError(
                    f"{iwhere}: group names must be non-empty strings"
                )
            if group in groups:
                raise ConfigError(f"{iwhere}: duplicate group {group!r}")
            groups.append(group)
            group_names.add(group)
        identities[name] = Identity(name=name, groups=tuple(groups))
    for name in identities:
        if name in group_names:
            raise ConfigError(
                f"{where}: {name!r} is both an identity and a group; names"
                " must be disjoint so grants stay unambiguous"
            )
    raw_grants = document.get("grants", [])
    if not isinstance(raw_grants, list):
        raise ConfigError(f"{where}: 'grants' must be an array")
    grants = []
    for index, item in enumerate(raw_grants, start=1):
        gwhere = f"{where} grant #{index}"
        if not isinstance(item, dict):
            raise ConfigError(f"{gwhere}: must be an object")
        subject = _required_text(item, "subject", gwhere)
        if subject not in identities and subject not in group_names:
            raise ConfigError(
                f"{gwhere}: subject {subject!r} is neither a registered"
                " identity nor a group used by one"
            )
        scheme = _required_text(item, "scheme", gwhere)
        identifier = _required_text(item, "identifier", gwhere)
        effect = item.get("effect")
        if effect not in _EFFECTS:
            raise ConfigError(
                f"{gwhere}: effect must be one of {', '.join(_EFFECTS)};"
                f" got {effect!r}"
            )
        grants.append(Grant(subject=subject, scheme=scheme,
                            identifier=identifier, effect=effect))
    return Policy(identities=identities, grants=tuple(grants))


def _required_text(obj: dict, key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: {key!r} must be a non-empty string")
    return value
