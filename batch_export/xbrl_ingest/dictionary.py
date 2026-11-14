"""Local concept dictionary: which {namespaceURI}localName concepts are allowed.

No taxonomy is downloaded. The dictionary is a small JSON file supplied by the
operator, for example::

    {
      "concepts": [
        {"namespace": "http://example.com/ifrs", "name": "Revenue", "type": "decimal"},
        {"namespace": "http://example.com/ifrs", "name": "Note",    "type": "text"}
      ]
    }

or a list of such objects, optionally using the keys ``localName``/``local``.
Each entry is registered by QName text ``{uri}local`` and typed decimal or text.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import ConceptSpec


class DictionaryError(ValueError):
    """The concept dictionary file is missing or invalid."""


def load_dictionary(source: str | Path | Mapping[str, Any] | Iterable[ConceptSpec]) -> dict[tuple[str, str], ConceptSpec]:
    """Load and validate a concept dictionary.

    Accepts an in-memory mapping/list of ConceptSpec, or a path to JSON. The
    returned mapping is keyed by (namespace URI, local name) and never relies on
    prefix spellings.
    """
    if isinstance(source, Mapping) or isinstance(source, (list, tuple)):
        return _from_objects(source)
    path = Path(source)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DictionaryError(f"concept dictionary not found: {path}") from None
    except (OSError, ValueError) as exc:
        raise DictionaryError(f"cannot read concept dictionary {path}: {exc}") from None
    if isinstance(payload, dict) and "concepts" in payload:
        payload = payload["concepts"]
    return _from_objects(payload)


def _from_objects(payload: Any) -> dict[tuple[str, str], ConceptSpec]:
    if isinstance(payload, ConceptSpec):
        payload = [payload]
    if isinstance(payload, Mapping):
        # { "{ns}local": "decimal" } or { "{ns}local": {"type": ...} }
        entries: list[Mapping[str, Any]] = []
        for qname, value in payload.items():
            entry = value if isinstance(value, Mapping) else {"type": value}
            namespace, local = _split_qname(qname)
            entries.append({**entry, "namespace": namespace, "name": local})
    else:
        entries = list(payload)
    if not entries:
        raise DictionaryError("concept dictionary is empty")
    result: dict[tuple[str, str], ConceptSpec] = {}
    for entry in entries:
        if isinstance(entry, ConceptSpec):
            spec = entry
        elif isinstance(entry, Mapping):
            namespace = entry.get("namespace", entry.get("uri"))
            name = entry.get("name", entry.get("localName", entry.get("local")))
            kind = entry.get("type")
            if namespace is None and entry.get("qname"):
                namespace, name = _split_qname(str(entry["qname"]))
            if not isinstance(namespace, str) or not isinstance(name, str):
                raise DictionaryError(f"concept entry needs namespace and name: {entry!r}")
            try:
                spec = ConceptSpec(namespace=namespace, local_name=name, type=str(kind))
            except (TypeError, ValueError) as exc:
                raise DictionaryError(f"invalid concept entry {entry!r}: {exc}") from None
        else:
            raise DictionaryError(f"invalid concept entry: {entry!r}")
        key = (spec.namespace, spec.local_name)
        if key in result:
            raise DictionaryError(f"concept {spec.qname} is registered more than once")
        result[key] = spec
    return result


def _split_qname(text: str) -> tuple[str, str]:
    if not text.startswith("{") or "}" not in text:
        raise DictionaryError(
            f"concept QName must be written {{namespaceURI}}localName, got {text!r}"
        )
    namespace, local = text[1:].split("}", 1)
    if not namespace or not local:
        raise DictionaryError(
            f"concept QName must be written {{namespaceURI}}localName, got {text!r}"
        )
    return namespace, local
