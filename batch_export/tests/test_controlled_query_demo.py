"""Runs the controlled-query acceptance demo under ``-I`` in a subprocess."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(
    PROJECT, "demo", "controlled_query_demo", "run_controlled_query_demo.py")


class TestDemoScript(unittest.TestCase):
    def test_demo_runs_under_isolated_mode(self):
        launcher = (
            "import sys, runpy, site\n"
            f"sys.path.insert(0, {PROJECT!r})\n"
            "site.addsitedir(site.getusersitepackages())\n"
            f"runpy.run_path({DEMO!r}, run_name='__main__')\n"
        )
        outcome = subprocess.run(
            [sys.executable, "-I", "-c", launcher],
            capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(outcome.returncode, 0,
                         outcome.stdout[-3000:] + outcome.stderr[-3000:])
        self.assertIn("checks held", outcome.stdout)
        self.assertIn("different schemes", outcome.stdout)
        self.assertIn("one read point", outcome.stdout)


if __name__ == "__main__":
    unittest.main()
