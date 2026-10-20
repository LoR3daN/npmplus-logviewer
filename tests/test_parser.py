import os
import shutil
import tempfile
import unittest

from app.parser import LogStore, parse_line


def make_line(ts, host="example.com", ip="203.0.113.10", status=200,
              body=100, nbytes=200, country="ES", ua="test-agent"):
    return (
        f'[20/Oct/2026:{ts} +0200] {host} {ip} 0.010 '
        f'"GET /{host} HTTP/1.1" {status} {body} {nbytes} "-" "{ua}" [{country}]\n'
    )


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="npmplus-test-")
        self.path = os.path.join(self.dir, "access_detailed.log")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, lines, path=None):
        with open(path or self.path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line)

    def append(self, text):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)

    def test_parse_line(self):
        rec = parse_line(make_line("23:00:01", host="proxy.lan:81",
                                   country="-"))
        self.assertEqual(rec["host"], "proxy.lan:81")
        self.assertEqual(rec["ip"], "203.0.113.10")
        self.assertEqual(rec["time_ms"], 10)
        self.assertEqual(rec["method"], "GET")
        self.assertEqual(rec["path"], "/proxy.lan:81")
        self.assertEqual(rec["status"], 200)
        self.assertEqual(rec["body"], 100)
        self.assertEqual(rec["bytes"], 200)
        self.assertEqual(rec["country"], "")

    def test_old_two_line_format_is_ignored(self):
        old = ('1.2.3.4 - - [20/Oct/2026:23:00:01 +0200] '
               '"GET / HTTP/1.1" 200 1 "-" "UA" [ES]')
        self.assertIsNone(parse_line(old))

    def test_scan_skips_legacy_lines(self):
        old = ('1.2.3.4 - - [20/Oct/2026:23:00:00 +0200] '
               '"GET /old HTTP/1.1" 200 1 "-" "UA" [ES]\n')
        self.write([old, make_line("23:00:01", host="new.example")])
        store = LogStore(self.dir)
        store.scan()
        self.assertEqual([r["host"] for r in store.records.values()],
                         ["new.example"])
        self.assertEqual(store.parse_errors, 0)

    def test_partial_line_is_kept(self):
        full = make_line("23:00:01")
        cut = len(full) // 2
        self.write([full[:cut]])
        store = LogStore(self.dir)
        store.scan()
        self.assertEqual(len(store.records), 0)
        self.append(full[cut:])
        store.scan()
        self.assertEqual(len(store.records), 1)

    def test_truncate_shorter_file(self):
        self.write([make_line("23:00:01", host="very-long-host.example.com")])
        store = LogStore(self.dir)
        store.scan()
        self.write([make_line("23:00:02", host="b.example")])
        store.scan()
        self.assertEqual(len(store.records), 2)

    def test_truncate_longer_file(self):
        self.write([make_line("23:00:01", host="a.example")])
        store = LogStore(self.dir)
        store.scan()
        self.write([
            make_line("23:00:02", host="b.example"),
            make_line("23:00:03", host="c.example"),
        ])
        store.scan()
        self.assertEqual(len(store.records), 3)

    def test_copytruncate_does_not_duplicate(self):
        self.write([
            make_line("23:00:01", host="a.example"),
            make_line("23:00:02", host="b.example"),
        ])
        store = LogStore(self.dir)
        store.scan()
        shutil.copyfile(self.path, os.path.join(
            self.dir, "access_detailed.log.1"))
        self.write([make_line("23:00:03", host="c.example")])
        store.scan()
        self.assertEqual(len(store.records), 3)

    def test_ignores_access_log_and_gz(self):
        self.write([make_line("23:00:01", host="a.example")])
        with open(os.path.join(self.dir, "access.log"), "w",
                  encoding="utf-8") as f:
            f.write(make_line("23:00:02", host="ignored.example"))
        with open(os.path.join(self.dir, "access_detailed.log.gz"), "w",
                  encoding="utf-8") as f:
            f.write("ignored")
        store = LogStore(self.dir)
        store.scan()
        self.assertEqual([r["host"] for r in store.records.values()],
                         ["a.example"])

    def test_live_collapse_groups_by_status(self):
        self.write([
            make_line("23:00:00", host="a.example", status=200),
            make_line("23:00:01", host="a.example", status=200),
            make_line("23:00:02", host="a.example", status=304),
        ])
        store = LogStore(self.dir)
        store.scan()
        live = store.query_live(collapse=True, limit=10)
        self.assertEqual([r["status"] for r in live], [200, 304])

    def test_query_live_returns_newest_record(self):
        self.write([make_line("23:00:02", host="new.example")])
        with open(os.path.join(self.dir, "access_detailed.log.1"), "w",
                  encoding="utf-8") as f:
            f.write(make_line("23:00:01", host="old.example"))
        store = LogStore(self.dir)
        store.scan()
        self.assertEqual([r["host"] for r in store.query_live(limit=1)],
                         ["new.example"])

    def test_collapse_groups_by_status(self):
        self.write([
            make_line("23:00:00", host="a.example", status=200),
            make_line("23:00:01", host="a.example", status=200),
            make_line("23:00:02", host="a.example", status=304),
            make_line("23:00:03", host="a.example", status=200),
            make_line("23:00:04", host="b.example", status=200),
        ])
        store = LogStore(self.dir)
        store.scan()
        out = store.query(collapse=True, sort="ts", order="desc", size=10)
        rows = [(r["host"], r["status"], r["count"], r["ts"])
                for r in out["items"]]
        self.assertEqual(out["total"], 4)
        self.assertEqual(rows[0], ("b.example", 200, 1,
                                   "2026-10-20T23:00:04+02:00"))
        self.assertEqual(rows[1], ("a.example", 200, 1,
                                   "2026-10-20T23:00:03+02:00"))
        self.assertEqual(rows[2], ("a.example", 304, 1,
                                   "2026-10-20T23:00:02+02:00"))
        self.assertEqual(rows[3], ("a.example", 200, 2,
                                   "2026-10-20T23:00:01+02:00"))

        out = store.query(collapse=True, sort="count", order="desc", size=10)
        self.assertEqual(out["items"][0]["count"], 2)

    def test_sort_by_host_and_bytes(self):
        self.write([
            make_line("23:00:01", host="b.example", body=500, nbytes=900),
            make_line("23:00:02", host="a.example", body=100, nbytes=300),
        ])
        store = LogStore(self.dir)
        store.scan()
        hosts = [r["host"] for r in
                 store.query(sort="host", order="asc", size=10)["items"]]
        self.assertEqual(hosts, ["a.example", "b.example"])
        sizes = [r["bytes"] for r in
                 store.query(sort="bytes", order="asc", size=10)["items"]]
        self.assertEqual(sizes, [300, 900])

    def test_rotation_is_chronological(self):
        self.write([make_line("23:00:02", host="new.example")])
        with open(os.path.join(self.dir, "access_detailed.log.1"), "w",
                  encoding="utf-8") as f:
            f.write(make_line("23:00:01", host="old.example"))
        store = LogStore(self.dir)
        store.scan()
        self.assertEqual([r["host"] for r in store.records.values()],
                         ["old.example", "new.example"])


if __name__ == "__main__":
    unittest.main()
