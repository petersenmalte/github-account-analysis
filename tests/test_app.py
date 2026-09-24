import unittest

from github_account_analysis.app import _since


class AppInputTests(unittest.TestCase):
    def test_timeframe_options_are_bounded_and_validate_unknown_values(self):
        self.assertIsNone(_since("all_available"))
        self.assertIsNotNone(_since("30_days"))
        with self.assertRaises(ValueError):
            _since("forever")
