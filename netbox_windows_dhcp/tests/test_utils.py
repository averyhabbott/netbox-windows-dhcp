"""Unit tests for the pure helpers in utils.py — no DB, no network."""

from django.test import SimpleTestCase

from ..utils import decompose_lease_lifetime, lease_lifetime_display


class LeaseLifetimeDisplayTests(SimpleTestCase):
    """lease_lifetime_display picks the largest *exact* unit."""

    def test_exact_days(self):
        self.assertEqual(lease_lifetime_display(259200), '3 Days')

    def test_singular_day(self):
        self.assertEqual(lease_lifetime_display(86400), '1 Day')

    def test_falls_through_to_hours(self):
        # 73 hours is not a whole number of days.
        self.assertEqual(lease_lifetime_display(262800), '73 Hours')

    def test_singular_hour(self):
        self.assertEqual(lease_lifetime_display(3600), '1 Hour')

    def test_exact_minutes(self):
        self.assertEqual(lease_lifetime_display(1800), '30 Minutes')

    def test_singular_minute(self):
        self.assertEqual(lease_lifetime_display(60), '1 Minute')

    def test_falls_through_to_seconds(self):
        # 90s = 1.5 min, not a whole number of minutes.
        self.assertEqual(lease_lifetime_display(90), '90 Seconds')

    def test_singular_second(self):
        self.assertEqual(lease_lifetime_display(1), '1 Second')

    def test_zero_and_negative(self):
        self.assertEqual(lease_lifetime_display(0), '0 Seconds')
        self.assertEqual(lease_lifetime_display(-5), '-5 Seconds')


class DecomposeLeaseLifetimeTests(SimpleTestCase):
    """decompose_lease_lifetime is the inverse split into (value, unit)."""

    def test_days(self):
        self.assertEqual(decompose_lease_lifetime(259200), (3, 'days'))

    def test_single_day(self):
        self.assertEqual(decompose_lease_lifetime(86400), (1, 'days'))

    def test_hours(self):
        self.assertEqual(decompose_lease_lifetime(262800), (73, 'hours'))

    def test_minutes(self):
        self.assertEqual(decompose_lease_lifetime(1800), (30, 'minutes'))

    def test_seconds(self):
        self.assertEqual(decompose_lease_lifetime(90), (90, 'seconds'))

    def test_zero_falls_to_seconds(self):
        self.assertEqual(decompose_lease_lifetime(0), (0, 'seconds'))

    def test_round_trip_for_clean_values(self):
        # For values that decompose to a clean unit, recomposing yields the original.
        unit_seconds = {'days': 86400, 'hours': 3600, 'minutes': 60, 'seconds': 1}
        for seconds in (1, 60, 90, 1800, 3600, 86400, 259200, 262800):
            value, unit = decompose_lease_lifetime(seconds)
            self.assertEqual(value * unit_seconds[unit], seconds, msg=f'seconds={seconds}')
