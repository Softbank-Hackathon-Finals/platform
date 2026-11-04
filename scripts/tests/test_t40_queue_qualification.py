"""Temporary queue-only failure fixture; never merged into develop."""
import unittest
from pathlib import Path


class QueueQualification(unittest.TestCase):
    def test_individually_green_prs_are_red_together(self):
        scripts = Path(__file__).resolve().parents[1]
        self.assertFalse((scripts / "t40_marker_a.txt").exists()
                         and (scripts / "t40_marker_b.txt").exists(),
                         "T40 intentional combined-PR failure")
