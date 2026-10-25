import os
import tempfile
import unittest

from quiz.core import add_fractions, store_and_load


class CoreTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add_fractions("1/2", "1/3"), "5/6")

    def test_sqlite_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(store_and_load(os.path.join(d, "t.db"), "3/4"), "3/4")

    def test_tkinter_importable(self):
        import tkinter  # noqa: F401  (no window is created)
        import tkinter.ttk  # noqa: F401
