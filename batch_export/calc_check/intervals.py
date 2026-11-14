"""Exact closed-interval arithmetic for the declared precision rule.

A reported value ``v`` with ``decimals = d`` means the closed interval
``[v - 0.5*10**(-d), v + 0.5*10**(-d)]``; ``decimals = INF`` means the point
interval ``[v, v]``. Everything here is computed with :class:`decimal.Decimal`
inside a context whose precision is derived from the operands themselves, so a
result can never be rounded by the default 28-digit context and no value ever
passes through binary ``float``.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Iterable, Optional


@dataclass(frozen=True)
class DecimalInterval:
    """A closed finite interval ``[lower, upper]`` with ``lower <= upper``."""

    lower: Decimal
    upper: Decimal

    def __post_init__(self) -> None:
        if not (self.lower.is_finite() and self.upper.is_finite()):
            raise ValueError("interval bounds must be finite")
        if self.lower > self.upper:
            raise ValueError(f"interval lower bound {self.lower} > upper bound {self.upper}")

    @property
    def is_point(self) -> bool:
        return self.lower == self.upper

    def as_text(self) -> dict:
        return {"lower": format(self.lower, "f"), "upper": format(self.upper, "f")}


@contextmanager
def exact_context(*groups: Iterable[Decimal]):
    """Run decimal arithmetic with enough precision for every operand.

    Decimal context precision counts significant digits. Addition of values far
    apart in magnitude needs a precision covering the whole place span, while
    multiplication needs the sum of operand lengths, so both bounds are taken.
    """
    decimals: list[Decimal] = []
    for group in groups:
        decimals.extend(group)
    need = 28
    if decimals:
        tops: list[int] = []
        bottoms: list[int] = []
        spans: list[int] = []
        for value in decimals:
            sign, digits, exponent = value.as_tuple()
            top = len(digits) - 1 + exponent
            bottom = exponent
            tops.append(top)
            bottoms.append(bottom)
            spans.append(top - bottom + 1)
        # Addition / subtraction precision.
        need = max(need, max(tops) - min(bottoms) + 9)
        # Multiplication precision (plus a constant safety margin).
        need = max(need, sum(spans) + 9)
    with localcontext() as ctx:
        ctx.prec = need
        yield ctx


def half_width(decimals: str) -> Optional[Decimal]:
    """``0.5 * 10**(-d)``; ``None`` for ``INF`` (a point interval)."""
    if decimals == "INF":
        return None
    d = int(decimals)  # validated upstream as a signed integer
    with exact_context([Decimal(1)]):
        return Decimal(1).scaleb(-d) / 2


def fact_interval(value: Decimal, decimals: str) -> DecimalInterval:
    """Build the closed interval represented by one reported decimal fact."""
    half = half_width(decimals)
    if half is None:
        return DecimalInterval(value, value)
    with exact_context([value, half]):
        return DecimalInterval(value - half, value + half)


def intersect_intervals(left: DecimalInterval, right: DecimalInterval) -> Optional[DecimalInterval]:
    """Intersection of two closed intervals; ``None`` when they do not meet."""
    with exact_context([left.lower, left.upper, right.lower, right.upper]):
        lower = max(left.lower, right.lower)
        upper = min(left.upper, right.upper)
    if lower > upper:
        return None
    return DecimalInterval(lower, upper)


def scale_interval(interval: DecimalInterval, weight: Decimal) -> DecimalInterval:
    """Multiply an interval by a weight; negative weights swap the bounds."""
    if weight == 0:
        raise ValueError("calculation arc weight must be non-zero")
    with exact_context([interval.lower, interval.upper, weight]):
        low = interval.lower * weight
        high = interval.upper * weight
    if weight < 0:
        low, high = high, low
    return DecimalInterval(low, high)


def add_intervals(intervals: Iterable[DecimalInterval]) -> DecimalInterval:
    """Interval sum: lower bounds add, upper bounds add (Minkowski sum)."""
    items = list(intervals)
    operands: list[Decimal] = []
    for item in items:
        operands.extend([item.lower, item.upper])
    with exact_context(operands):
        lower = sum((item.lower for item in items), Decimal(0))
        upper = sum((item.upper for item in items), Decimal(0))
    return DecimalInterval(lower, upper)


def intervals_overlap(left: DecimalInterval, right: DecimalInterval) -> bool:
    """Intersection is non-empty; intervals touching only at an endpoint count."""
    return left.lower <= right.upper and right.lower <= left.upper
