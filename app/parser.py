"""Parser for NPMplus access.log files.

Each request generates TWO lines in the same file:
  A) proxy_host format: [date] host ip time "request" status body bytes referer ua
  B) standard nginx format + country: ip - - [date] "request" status body "referer" "ua" [COUNTRY]

Both are matched and merged into a single record (dedup by
date+IP+request+status+body).
"""

import ipaddress
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from .geo import cc_latlon

LOCAL_TZ = ZoneInfo(os.environ.get("TZ", "Europe/Madrid"))

# [14/Aug/2026:21:54:54 +0200] 192.168.X.XXX:81 192.168.X.XXX 0.023 "GET / HTTP/1.1" 200 1470 3369 - Uptime-Kuma/2.5.0
FORMAT_A = re.compile(
    r"^\[(?P<time>[^\]]+)\]\s+"
    r"(?P<host>\S+)\s+"
    r"(?P<ip>\S+)\s+"
    r"(?P<rt>\d+\.\d+)\s+"
    r"\"(?P<request>[^\"]*)\"\s+"
    r"(?P<status>\d+)\s+"
    r"(?P<body>\d+)\s+"
    r"(?P<bytes>\d+)\s+"
    r"(?P<referer>\"[^\"]*\"|-)\s+"
    r"(?P<ua>.+)$"
)

# 192.168.X.XXX - - [14/Aug/2026:21:54:54 +0200] "GET / HTTP/1.1" 200 1470 "-" "Uptime-Kuma/2.5.0" [ES]
FORMAT_B = re.compile(
    r"^(?P<ip>\S+)\s+-\s+(?P<user>\S+)\s+"
    r"\[(?P<time>[^\]]+)\]\s+"
    r"\"(?P<request>[^\"]*)\"\s+"
    r"(?P<status>\d+)\s+"
    r"(?P<body>\d+)\s+"
    r"\"(?P<referer>[^\"]*)\"\s+"
    r"\"(?P<ua>[^\"]*)\""
    r"(?:\s+\[(?P<country>[^\]]*)\])?$"
)

_TIME_FMT = "%d/%b/%Y:%H:%M:%S %z"


def parse_time(ts):
    dt = datetime.strptime(ts, _TIME_FMT)
    return dt, dt.timestamp()


_METHOD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


def split_request(req):
    parts = req.split()
    if not parts:
        return "-", "-", "-"
    method = parts[0]
    # Reject junk (TLS/SSH/SMB/scan payloads) that is not a real HTTP method.
    if not _METHOD_RE.match(method):
        method = "-"
    http = parts[-1] if len(parts) > 1 and parts[-1].startswith("HTTP/") else "-"
    if len(parts) >= 3 and http != "-":
        path = " ".join(parts[1:-1])
    elif len(parts) >= 2:
        path = parts[1]
    else:
        path = "-"
    return method, path, http


def parse_line(line):
    """Return a unified record, or None if the line is not recognized."""
    line = line.rstrip("\n")
    if not line:
        return None

    m = FORMAT_B.match(line)
    if m:
        g = m.groupdict()
        dt, epoch = parse_time(g["time"])
        method, path, http = split_request(g["request"])
        country = g.get("country") or ""
        if country == "-":
            country = ""
        return {
            "kind": "B",
            "ts": dt.isoformat(),
            "epoch": epoch,
            "host": "",
            "ip": g["ip"],
            "time_ms": None,
            "method": method,
            "path": path,
            "http": http,
            "status": int(g["status"]),
            "body": int(g["body"]),
            "bytes": None,
            "referer": g["referer"],
            "ua": g["ua"],
            "country": country,
        }

    m = FORMAT_A.match(line)
    if m:
        g = m.groupdict()
        dt, epoch = parse_time(g["time"])
        method, path, http = split_request(g["request"])
        referer = g["referer"].strip('"')
        return {
            "kind": "A",
            "ts": dt.isoformat(),
            "epoch": epoch,
            "host": g["host"],
            "ip": g["ip"],
            "time_ms": int(round(float(g["rt"]) * 1000)),
            "method": method,
            "path": path,
            "http": http,
            "status": int(g["status"]),
            "body": int(g["body"]),
            "bytes": int(g["bytes"]),
            "referer": referer,
            "ua": g["ua"],
            "country": "",
        }
    return None


def _key(rec):
    return (rec["ts"], rec["ip"], rec["method"], rec["path"],
            rec["status"], rec["body"])


def _is_local_ip(ip):
    """True if the IP is private / loopback / link-local / local network."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_unspecified or addr.is_reserved)


class LogStore:
    """Keeps records in memory and scans the logs incrementally.

    On startup (empty memory) it does a full read of all access.log files.
    Afterwards it only reads the new bytes of each file (tracked by
    inode + size), avoiding duplicates when logrotate renames files
    (the inode does not change on rotation).
    """

    def __init__(self, log_dir, max_entries=300_000):
        self.log_dir = log_dir
        self.max_entries = max_entries
        self._lock = threading.RLock()
        self.records = {}
        self.order = []
        self.c_host = Counter()
        self.c_status = Counter()
        self.c_method = Counter()
        self.c_country = Counter()
        self.state = {}
        self.last_scan = None
        self.files_info = []

    # ---- scanning ------------------------------------------------------------
    def files(self):
        if not os.path.isdir(self.log_dir):
            return []
        out = []
        for name in os.listdir(self.log_dir):
            if not name.startswith("access.log") or name.endswith(".gz"):
                continue
            full = os.path.join(self.log_dir, name)
            if os.path.isfile(full):
                out.append((name, full))

        def sort_key(item):
            name = item[0]
            if name == "access.log":
                return (0, 0)
            try:
                return (1, int(name.split(".", 1)[-1]))
            except ValueError:
                return (2, name)

        out.sort(key=sort_key)
        return out

    def scan(self):
        if not os.path.isdir(self.log_dir):
            self.last_scan = time.time()
            return
        with self._lock:
            info = []
            for name, full in self.files():
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                inode = st.st_ino
                size = st.st_size
                prev = self.state.get(inode)
                if prev is not None and prev["size"] == size:
                    info.append({"file": name, "inode": inode, "size": size})
                    continue
                offset = min(prev["size"], size) if prev else 0
                with open(full, "rb") as f:
                    if offset:
                        f.seek(offset)
                    data = f.read()
                self.state[inode] = {"file": name, "size": size}
                for line in data.decode("utf-8", "replace").splitlines():
                    rec = parse_line(line)
                    if rec:
                        self._add(rec)
                info.append({"file": name, "inode": inode, "size": size})
            self.files_info = info
            self.last_scan = time.time()

    # ---- insert / dedup -----------------------------------------------------
    def _add(self, rec):
        key = _key(rec)
        existing = self.records.get(key)
        if existing is None:
            self.records[key] = rec
            self.order.append(key)
            self._count(rec, 1)
            while len(self.order) > self.max_entries:
                old = self.order.pop(0)
                gone = self.records.pop(old, None)
                if gone:
                    self._count(gone, -1)
        elif rec["kind"] == "A" and existing["kind"] == "B":
            self._host_delta(existing, -1)
            existing["host"] = rec["host"]
            existing["time_ms"] = rec["time_ms"]
            existing["bytes"] = rec["bytes"]
            self._host_delta(existing, 1)
        elif rec["kind"] == "B" and existing["kind"] == "A":
            if rec["country"] and rec["country"] != existing["country"]:
                if existing["country"]:
                    self._country_delta(existing, -1)
                existing["country"] = rec["country"]
                self._country_delta(existing, 1)
            if rec["ua"]:
                existing["ua"] = rec["ua"]

    def _host_delta(self, rec, delta):
        host = rec["host"] or "—"
        self.c_host[host] += delta
        if self.c_host[host] <= 0:
            del self.c_host[host]

    def _country_delta(self, rec, delta):
        if not rec["country"]:
            return
        self.c_country[rec["country"]] += delta
        if self.c_country[rec["country"]] <= 0:
            del self.c_country[rec["country"]]

    def _count(self, rec, delta):
        self._host_delta(rec, delta)
        self.c_status[rec["status"]] += delta
        if self.c_status[rec["status"]] <= 0:
            del self.c_status[rec["status"]]
        self.c_method[rec["method"]] += delta
        if self.c_method[rec["method"]] <= 0:
            del self.c_method[rec["method"]]
        self._country_delta(rec, delta)

    # ---- queries ------------------------------------------------------------
    def stats(self):
        by_status = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
        for code, n in self.c_status.items():
            bucket = f"{code // 100}xx"
            by_status[bucket] = by_status.get(bucket, 0) + n
        return {
            "total": len(self.records),
            "by_status": by_status,
            "statuses": [{"value": k, "count": v}
                         for k, v in sorted(self.c_status.items())],
            "hosts": [{"value": k, "count": v}
                      for k, v in self.c_host.most_common()],
            "methods": [{"value": k, "count": v}
                        for k, v in self.c_method.most_common()],
            "countries": [{"value": k, "count": v}
                          for k, v in self.c_country.most_common()],
            "last_scan": self.last_scan,
            "files": self.files_info,
        }

    def _filtered(self, host=None, status=None, method=None, country=None,
                  ip=None, q=None, exclude_ua=None, exclude_local=False,
                  from_epoch=None, to_epoch=None):
        """Return records matching the filters, oldest first."""
        exclude_ua = [x.lower() for x in (exclude_ua or [])]
        host = host or []
        status = status or []
        method = method or []
        country = country or []
        items = []
        for key in self.order:
            r = self.records[key]
            if host and r["host"] not in host:
                continue
            if status:
                ok = False
                for s in status:
                    if s.endswith("xx"):
                        base = int(s[0]) * 100
                        if base <= r["status"] < base + 100:
                            ok = True
                            break
                    elif r["status"] == int(s):
                        ok = True
                        break
                if not ok:
                    continue
            if method and r["method"] not in method:
                continue
            if country and r["country"] not in country:
                continue
            if ip and ip not in r["ip"]:
                continue
            if exclude_local and _is_local_ip(r["ip"]):
                continue
            if q:
                hay = " ".join([r["ip"], r["host"], r["method"], r["path"],
                                r["ua"], r["referer"]]).lower()
                if q.lower() not in hay:
                    continue
            if exclude_ua:
                ua_low = r["ua"].lower()
                if any(x and x in ua_low for x in exclude_ua):
                    continue
            if from_epoch is not None and r["epoch"] < from_epoch:
                continue
            if to_epoch is not None and r["epoch"] > to_epoch:
                continue
            items.append(r)
        return items

    def query(self, host=None, status=None, method=None, country=None,
              ip=None, q=None, exclude_ua=None, exclude_local=False,
              from_epoch=None, to_epoch=None,
              sort="ts", order="desc", page=1, size=50, collapse=False):
        items = self._filtered(
            host=host, status=status, method=method, country=country,
            ip=ip, q=q, exclude_ua=exclude_ua, exclude_local=exclude_local,
            from_epoch=from_epoch, to_epoch=to_epoch,
        )

        if sort == "status":
            items.sort(key=lambda r: r["status"], reverse=(order == "desc"))
        elif sort == "time":
            items.sort(key=lambda r: r["time_ms"] if r["time_ms"] is not None else -1,
                       reverse=(order == "desc"))
        elif sort == "body":
            items.sort(key=lambda r: r["body"], reverse=(order == "desc"))
        else:
            items.sort(key=lambda r: r["epoch"], reverse=(order == "desc"))

        # Simplified view: collapse consecutive runs of requests from the
        # same IP to the same domain into a single row.
        if collapse:
            collapsed = self._collapse_runs(items)
            total = len(collapsed)
            start = (page - 1) * size
            out = []
            for r, count in collapsed[start:start + size]:
                row = self._to_row(r)
                row["count"] = count
                out.append(row)
            return {"total": total, "page": page, "size": size, "items": out}

        total = len(items)
        start = (page - 1) * size
        out = [self._to_row(r) for r in items[start:start + size]]
        return {"total": total, "page": page, "size": size, "items": out}

    @staticmethod
    def _collapse_runs(items):
        """Collapse consecutive records with the same (host, ip) into runs.

        Returns a list of (last_record_of_run, run_length). The epoch of the
        returned record is the latest one of the run, so incremental live
        polling still sees a run once it finishes.
        """
        runs = []
        last_key = None
        run = None
        for r in items:
            key = (r["host"], r["ip"])
            if key != last_key:
                run = [r, 1]
                runs.append(run)
                last_key = key
            else:
                run[1] += 1
                if r["epoch"] > run[0]["epoch"]:
                    run[0] = r
        return runs

    @staticmethod
    def _to_row(r):
        return {
            "ts": r["ts"],
            "epoch": r["epoch"],
            "host": r["host"] or "—",
            "ip": r["ip"],
            "time_ms": r["time_ms"],
            "method": r["method"],
            "path": r["path"],
            "http": r["http"],
            "status": r["status"],
            "body": r["body"],
            "bytes": r["bytes"],
            "referer": r["referer"],
            "ua": r["ua"],
            "country": r["country"],
        }

    def query_live(self, after_epoch=None, limit=200, collapse=False, **kwargs):
        """Recent records with known coordinates for the map view.

        Reuses the same filters as query(). Only records whose country has a
        known centroid are returned, with lat/lon attached. With after_epoch
        set, only records newer than it are returned (incremental polling).
        With collapse, consecutive runs from the same (host, ip) are reduced
        to one beam.
        """
        items = self._filtered(**kwargs)
        if collapse:
            items = [run[0] for run in self._collapse_runs(items)]
        if after_epoch is not None:
            items = [r for r in items if r["epoch"] > after_epoch]
        out = []
        for r in items[-limit:]:
            pos = cc_latlon(r["country"])
            if pos is None:
                continue
            out.append({
                "epoch": r["epoch"],
                "cc": r["country"],
                "lat": pos[0],
                "lon": pos[1],
                "host": r["host"] or "—",
                "method": r["method"],
                "path": r["path"],
                "status": r["status"],
                "ip": r["ip"],
                "ua": r["ua"],
            })
        return out

    def hosts_for_public_ip(self, ip):
        """Hosts matching the server's public IP, incl. port 80 and 443."""
        if not ip:
            return []
        variants = [ip, f"{ip}:80", f"{ip}:443"]
        counts = Counter()
        for key in self.order:
            r = self.records[key]
            if r["host"] in variants:
                counts[r["host"]] += 1
        return [{"value": v, "count": counts.get(v, 0)} for v in variants]

    def _host_count(self, host):
        n = 0
        for key in self.order:
            if self.records[key]["host"] == host:
                n += 1
        return n

    def detect_public_ip_from_logs(self):
        """Best guess of the server's current public IP.

        The server's public IP appears in the logs as a proxy host with
        :80 and :443 variants (IP / IP:80 / IP:443). Returns the most
        recent such public IP.
        """
        seen = set()
        for key in reversed(self.order):
            r = self.records[key]
            host = r["host"] or ""
            if ":" not in host:
                continue
            h, _, p = host.rpartition(":")
            if p not in ("80", "443"):
                continue
            if h in seen:
                continue
            seen.add(h)
            try:
                addr = ipaddress.ip_address(h)
            except ValueError:
                continue
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                continue
            if (self._host_count(h) or self._host_count(f"{h}:80")
                    or self._host_count(f"{h}:443")):
                return h
        return ""
