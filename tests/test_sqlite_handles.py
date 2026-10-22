"""Every SQLite connection the structured-log store opens is closed.

``with sqlite3.connect(...)`` only commits; the open connection kept
``structured_logs.sqlite`` locked, so Windows could not delete a run directory
(40 Windows CI failures, WinError 32).
"""

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from crypto_stat_arb import structured_logging
from crypto_stat_arb.runner import run_backtest

ROOT = Path(__file__).parents[1]


class SqliteHandleTests(unittest.TestCase):
    def test_backtest_leaves_no_structured_log_connection_open(self):
        opened = []
        real_connect = sqlite3.connect

        def tracking_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(structured_logging.sqlite3, "connect", tracking_connect):
            run_backtest(ROOT / "config/research.json", ROOT / "data/samples/two_venue_books.jsonl",
                         Path(directory) / "run")
            self.assertTrue(opened)
            still_open = []
            for connection in opened:
                try:
                    connection.cursor()
                    still_open.append(connection)
                except sqlite3.ProgrammingError:       # "Cannot operate on a closed database."
                    pass
            self.assertEqual(still_open, [])
            (Path(directory) / "run" / "structured_logs.sqlite").unlink()    # not locked

    def test_failed_write_still_closes_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.sqlite"
            setup = sqlite3.connect(path)
            setup.execute("create table t (v integer)")      # DDL autocommits in sqlite3's legacy mode
            setup.close()
            with self.assertRaises(sqlite3.OperationalError):
                with structured_logging._connect(path) as connection:
                    connection.execute("insert into t values (1)")
                    connection.execute("not valid sql")
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.cursor()
            fresh = sqlite3.connect(path)
            self.assertEqual(fresh.execute("select count(*) from t").fetchone(), (0,))   # insert rolled back
            fresh.close()


if __name__ == "__main__":
    unittest.main()
