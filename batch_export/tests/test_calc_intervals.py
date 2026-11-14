"""Unit tests for the exact closed-interval arithmetic.

Expected bounds are written by hand from the rule
``[v - 0.5*10^(-d), v + 0.5*10^(-d)]`` and never derived by importing the
production interval helpers into the expected values.
"""
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, __import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from calc_check.intervals import (  # noqa: E402
    DecimalInterval,
    add_intervals,
    fact_interval,
    intersect_intervals,
    intervals_overlap,
    scale_interval,
)


class TestFactIntervals(unittest.TestCase):
    def test_inf_is_a_point_interval(self):
        interval = fact_interval(Decimal("12.5"), "INF")
        self.assertEqual(interval, DecimalInterval(Decimal("12.5"), Decimal("12.5")))
        self.assertTrue(interval.is_point)

    def test_positive_decimals_half_width_hand_derived(self):
        # d=2 -> half width 0.005
        self.assertEqual(
            fact_interval(Decimal("1.25"), "2"),
            DecimalInterval(Decimal("1.245"), Decimal("1.255")),
        )

    def test_negative_decimals_half_width_hand_derived(self):
        # d=-2 -> half width 50
        self.assertEqual(
            fact_interval(Decimal("100"), "-2"),
            DecimalInterval(Decimal("50"), Decimal("150")),
        )
        # d=-3 -> half width 500
        self.assertEqual(
            fact_interval(Decimal("10000"), "-3"),
            DecimalInterval(Decimal("9500"), Decimal("10500")),
        )


class TestIntersection(unittest.TestCase):
    def test_endpoint_contact_intersects_at_a_point(self):
        left = DecimalInterval(Decimal("0"), Decimal("10"))
        right = DecimalInterval(Decimal("10"), Decimal("20"))
        self.assertEqual(intersect_intervals(left, right),
                         DecimalInterval(Decimal("10"), Decimal("10")))
        self.assertTrue(intervals_overlap(left, right))

    def test_disjoint_returns_none(self):
        left = DecimalInterval(Decimal("0"), Decimal("9.9"))
        right = DecimalInterval(Decimal("10"), Decimal("20"))
        self.assertIsNone(intersect_intervals(left, right))
        self.assertFalse(intervals_overlap(left, right))

    def test_nested_intersection_is_the_inner(self):
        outer = DecimalInterval(Decimal("0"), Decimal("100"))
        inner = DecimalInterval(Decimal("40"), Decimal("60"))
        self.assertEqual(intersect_intervals(outer, inner), inner)


class TestScalingAndSums(unittest.TestCase):
    def test_positive_weight_keeps_bounds(self):
        interval = DecimalInterval(Decimal("1"), Decimal("2"))
        self.assertEqual(scale_interval(interval, Decimal("3")),
                         DecimalInterval(Decimal("3"), Decimal("6")))

    def test_negative_weight_swaps_bounds(self):
        interval = DecimalInterval(Decimal("1"), Decimal("2"))
        self.assertEqual(scale_interval(interval, Decimal("-1")),
                         DecimalInterval(Decimal("-2"), Decimal("-1")))

    def test_zero_weight_is_rejected(self):
        with self.assertRaises(ValueError):
            scale_interval(DecimalInterval(Decimal("0"), Decimal("1")), Decimal("0"))

    def test_interval_sum_hand_derived(self):
        first = fact_interval(Decimal("50.5"), "1")   # [50.45, 50.55]
        second = fact_interval(Decimal("49.9"), "1")  # [49.85, 49.95]
        self.assertEqual(add_intervals([first, second]),
                         DecimalInterval(Decimal("100.30"), Decimal("100.50")))

    def test_negative_weighted_sum_hand_derived(self):
        gross = fact_interval(Decimal("1500"), "-2")   # [1450, 1550]
        returned = fact_interval(Decimal("200"), "-2")  # [150, 250]
        total = add_intervals([gross, scale_interval(returned, Decimal("-1"))])
        self.assertEqual(total, DecimalInterval(Decimal("1200"), Decimal("1400")))


class TestExactness(unittest.TestCase):
    def test_forty_digit_values_not_rounded_like_default_context(self):
        # Under the default 28-digit context, multiplying/re-adding these would
        # round; exact arithmetic must preserve every digit.
        big_a = Decimal("1000000000000000000000000000000000000000")
        big_b = Decimal("234567890123456789012345678901234567890")
        point_a = fact_interval(big_a, "INF")
        point_b = fact_interval(big_b, "INF")
        total = add_intervals([point_a, point_b])
        self.assertEqual(
            total.lower,
            Decimal("1234567890123456789012345678901234567890"),
        )
        self.assertEqual(
            total.upper,
            Decimal("1234567890123456789012345678901234567890"),
        )

    def test_adjacent_point_intervals_at_40th_digit_are_disjoint(self):
        one = fact_interval(
            Decimal("1234567890123456789012345678901234567890"), "INF")
        two = fact_interval(
            Decimal("1234567890123456789012345678901234567891"), "INF")
        self.assertFalse(intervals_overlap(one, two))

    def test_small_fractions_keep_full_scale(self):
        tiny = fact_interval(Decimal("0.000000000001"), "12")
        self.assertEqual(tiny.lower, Decimal("0.0000000000005"))
        self.assertEqual(tiny.upper, Decimal("0.0000000000015"))


if __name__ == "__main__":
    unittest.main()
