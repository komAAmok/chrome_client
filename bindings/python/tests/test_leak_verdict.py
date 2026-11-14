"""The leak gate must call a leak a leak, at every batch size it is run at.

``tools/leak-hunt.py`` had a verdict that could not fire: its "late window" --
the second half of the series, where a converging cache is already flat and a
leak is still climbing -- was computed from the number of BATCHES and then used
to slice the list AFTER ``_floor()`` had shortened it by one. Six batches became
five points, half of six is three, and the window that was meant to hold three
points held two. Two points always fit a line exactly, so the discriminator
carried no information, and the absolute floor of three descriptors was then the
only thing left. Measured: a series rising by three descriptors per batch -- a
real, unbounded leak -- and a series that merely opened one connection at the
end both reported "flat".

These tests pin the property rather than the numbers: a rising series must be
reported, a one-off step must not, and raising the batch size must not change
either answer.
"""

import importlib.util
import pathlib
import unittest


def _leak_hunt():
    """Loads tools/leak-hunt.py as a module without running its main()."""
    path = pathlib.Path(__file__).resolve().parents[3] / "tools" / "leak-hunt.py"
    spec = importlib.util.spec_from_file_location("leak_hunt_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LateWindowTests(unittest.TestCase):
    """The late window is what separates a leak from a cache filling up."""

    def setUp(self):
        self.leak = _leak_hunt()

    def _judge(self, series, requests_per_batch):
        """Calls the real judge(), so a regression in it fails this test."""
        samples = [{"fds": value, "rss_kib": 1000, "threads": 4,
                    "fd_kinds": {"socket": value}} for value in series]
        return self.leak.judge(samples, requests_per_batch)["verdict"]

    def test_the_late_window_keeps_three_points(self):
        """Two points fit any line exactly, so they cannot judge anything."""
        self.assertGreaterEqual(self.leak.LATE_WINDOW, 3)
        for batches in (4, 5, 6, 8, 12):
            series = [9 + index for index in range(batches)]
            record = self.leak.judge(
                [{"fds": value, "rss_kib": 1000, "threads": 4,
                  "fd_kinds": {"socket": value}} for value in series], 20)
            # The late window is not reported directly, so the property is read
            # through the verdict: a series rising by one descriptor per batch
            # costs `batches` requests per batch, which is a real rate.
            self.assertNotEqual(record["verdict"], "unstable:flat/flat")
            half = max(0, len(record["fd_series"])
                       - max(self.leak.LATE_WINDOW, len(record["fd_series"]) // 2))
            self.assertGreaterEqual(len(record["fd_series"][half:]), 3,
                                    "%d batches left a %d-point late window"
                                    % (batches, len(record["fd_series"][half:])))

    def test_a_per_request_leak_is_reported(self):
        """Three descriptors per batch for six batches is unbounded growth.

        This is the case the old arithmetic missed: _floor() turns six samples
        into five, so a late window computed from len(samples) held two points
        and always looked flat.
        """
        self.assertEqual(self._judge([9, 12, 15, 18, 21, 24], 20), "grows:fd")

    def test_a_single_late_connection_is_not_a_leak(self):
        """One extra descriptor at the end is a pooled connection, not a rate."""
        self.assertEqual(self._judge([9, 9, 9, 9, 9, 12], 20), "flat")

    def test_a_converging_cache_is_not_a_leak(self):
        self.assertEqual(self._judge([9, 12, 15, 15, 15, 15], 20), "flat")

    def test_the_verdict_does_not_depend_on_the_batch_size(self):
        """The same rate reported at 20 and at 40 requests per batch."""
        self.assertEqual(self._judge([9, 12, 15, 18, 21, 24], 20), "grows:fd")
        self.assertEqual(self._judge([9, 11, 13, 15, 17], 40), "grows:fd")

    def test_a_flat_series_stays_flat(self):
        for batches in (5, 6, 8):
            self.assertEqual(self._judge([9] * batches, 20), "flat")


class AbsoluteFloorTests(unittest.TestCase):
    """The floors scale with the batch size, and never below the minimums."""

    def setUp(self):
        self.leak = _leak_hunt()

    def test_floors_never_go_below_one_connection(self):
        for count in (1, 5, 20, 40, 100, 400):
            self.leak.set_requests_per_batch(count)
            fd_floor, rss_floor, thread_floor = self.leak.absolute_floors()
            self.assertGreaterEqual(fd_floor, 3)
            self.assertGreaterEqual(rss_floor, 2048)
            self.assertGreaterEqual(thread_floor, 2)

    def test_a_smaller_batch_gets_a_smaller_floor(self):
        """A rate that moves a small batch by one descriptor must be visible."""
        self.leak.set_requests_per_batch(40)
        big = self.leak.absolute_floors()
        self.leak.set_requests_per_batch(10)
        small = self.leak.absolute_floors()
        self.assertLessEqual(small[0], big[0])
        self.assertLessEqual(small[1], big[1])


if __name__ == "__main__":
    unittest.main()
