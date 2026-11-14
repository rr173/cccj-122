"""Namespace-aware QName handling and strict lexical parsers.

The ingester never infers a namespace from a *type* or a *dictionary*; every
QName (element name, explicitMember dimension/member, measure) is expanded with
the prefix bindings in force at the XML element that carries it.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from .errors import XbrlXmlError, InvalidValueError, SourceLocation

_NS_SEP = "\x01"
_DECIMALS_RE = re.compile(r"^[+-]?\d+$")
_DECIMAL_LEXICAL_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def split_expanded(name: str) -> tuple[Optional[str], str]:
    """Split an expat namespace-separated name into (uri or None, local)."""
    if _NS_SEP in name:
        uri, local = name.split(_NS_SEP, 1)
        return uri, local
    return None, name


def parse_qname_text(
    text: str,
    prefixes: dict[str, str],
    default_ns: Optional[str],
    location: SourceLocation,
    *,
    what: str,
) -> tuple[str, str]:
    """Expand a QName written as text (explicitMember, measure).

    An unprefixed QName uses the default namespace in force at that element.
    A prefixed QName must have a binding in force at that element; the exact
    prefix string is *not* part of the resulting identity.
    """
    raw = text.strip()
    if not raw:
        raise InvalidValueError(f"{what}: empty QName", location=location)
    if raw.count(":") > 1:
        raise InvalidValueError(f"{what}: not a QName: {text!r}", location=location)
    if ":" in raw:
        prefix, local = raw.split(":", 1)
        if not prefix or not local:
            raise InvalidValueError(f"{what}: not a QName: {text!r}", location=location)
        if prefix not in prefixes:
            raise XbrlXmlError(
                f"{what}: prefix {prefix!r} is not bound here (QName {text!r})",
                location=location,
            )
        uri = prefixes[prefix]
        if not uri:
            raise XbrlXmlError(
                f"{what}: prefix {prefix!r} is bound to the empty namespace",
                location=location,
            )
        return uri, local
    if not default_ns:
        raise XbrlXmlError(
            f"{what}: unprefixed QName {text!r} has no default namespace here",
            location=location,
        )
    return default_ns, raw


def validate_date(text: str, location: SourceLocation, what: str) -> str:
    """Accept only a legal YYYY-MM-DD calendar date, returned unchanged."""
    if text != text.strip():
        raise InvalidValueError(
            f"{what}: date {text!r} has surrounding whitespace", location=location
        )
    raw = text
    if len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
        raise InvalidValueError(
            f"{what}: date {text!r} is not YYYY-MM-DD", location=location
        )
    digits = raw[0:4] + raw[5:7] + raw[8:10]
    if not all("0" <= ch <= "9" for ch in digits):
        raise InvalidValueError(
            f"{what}: date {text!r} is not YYYY-MM-DD", location=location
        )
    year, month, day = int(raw[0:4]), int(raw[5:7]), int(raw[8:10])
    if year < 1:
        raise InvalidValueError(
            f"{what}: year 0000 is not a legal date in {text!r}", location=location
        )
    if not (1 <= month <= 12):
        raise InvalidValueError(f"{what}: month out of range in {text!r}", location=location)
    leap = (year % 4 == 0 and year % 100 != 0) or year % 400 == 0
    month_days = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if not (1 <= day <= month_days[month - 1]):
        raise InvalidValueError(f"{what}: day out of range in {text!r}", location=location)
    return text


def validate_decimals(text: str, location: SourceLocation) -> str:
    """decimals is a signed integer or the literal INF, e.g. 2, +3, -1, INF."""
    raw = text.strip()
    if raw == "INF":
        return raw
    if not _DECIMALS_RE.match(raw):
        raise InvalidValueError(
            f"decimal fact: invalid decimals {text!r} (want signed integer or INF)",
            location=location,
        )
    return raw


def parse_decimal(text: str, location: SourceLocation) -> Decimal:
    """Parse an xsd:decimal lexical form exactly; reject NaN/INF/underscores."""
    stripped = text.strip()
    if not stripped or not _DECIMAL_LEXICAL_RE.match(stripped):
        raise InvalidValueError(
            f"decimal fact: invalid value {text!r}", location=location
        )
    try:
        value = Decimal(stripped)
    except InvalidOperation:
        raise InvalidValueError(
            f"decimal fact: invalid value {text!r}", location=location
        ) from None
    if not value.is_finite():
        raise InvalidValueError(
            f"decimal fact: non-finite value {text!r}", location=location
        )
    return value


def canonical_decimal_text(value: Decimal) -> str:
    """Comparable decimal form that ignores scale (1.0 and 1.00 are equal)."""
    if value.is_zero():
        return "0"
    sign, digits, exponent = value.as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    digits_text = "".join(str(d) for d in digits)
    prefix = "-" if sign else ""
    if exponent >= 0:
        return f"{prefix}{digits_text}{'0' * exponent}"
    point = len(digits_text) + exponent
    if point > 0:
        return f"{prefix}{digits_text[:point]}.{digits_text[point:]}"
    return f"{prefix}0.{'0' * (-point)}{digits_text}"
