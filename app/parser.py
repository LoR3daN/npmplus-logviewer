"""Parser for NPMplus access_detailed.log files.

One line per request:
  [date] host ip response_time "request" status body_bytes bytes_sent "referer" "ua" [COUNTRY]
"""

import ipaddress
import logging
import os
import re
import threading
import time
from collections import Counter, deque
from datetime import datetime
from zoneinfo import ZoneInfo

from .geo import cc_latlon

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo(os.environ.get("TZ", "Europe/Madrid"))

LOG_PREFIX = "access_detailed.log"

# [14/Aug/2026:21:54:54 +0200] example.com 192.168.X.XXX 0.023 "GET / HTTP/1.1" 200 1470 3369 "-" "Uptime-Kuma/2.5.0" [ES]
FORMAT = re.compile(
    r"^\[(?P<time>[^\]]+)\]\s+"
    r"(?P<host>\S+)\s+"
    r"(?P<ip>\S+)\s+"
    r"(?P<rt>\d+\.\d+)\s+"
    r"\"(?P<request>[^\"]*)\"\s+"
    r"(?P<status>\d+)\s+"
    r"(?P<body>\d+)\s+"
    r"(?P<bytes>\d+)\s+"
    r"\"(?P<referer>[^\"]*)\"\s+"
    r"\"(?P<ua>[^\"]*)\"\s+"
    r"\[(?P<country>[^\]]*)\]$"
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
    """Return a record, or None if the line is not recognized."""
    line = line.rstrip("\r\n")
    if not line:
        return None

    m = FORMAT.match(line)
    if not m:
        return None

    g = m.groupdict()
    dt, epoch = parse_time(g["time"])
    method, path, http = split_request(g["request"])
    country = g["country"]
    if country == "-":
        country = ""
    return {
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
        "referer": g["referer"],
        "ua": g["ua"],
        "country": country,
    }


def _key(rec):
    return (rec["ts"], rec["ip"], rec["host"], rec["method"], rec["path"],
            rec["status"], rec["body"], rec["bytes"], rec["time_ms"])


def _is_local_ip(ip):
    """True if the IP is private / loopback / link-local / local network."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_unspecified or addr.is_reserved)


def _ip_sort_key(value):
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return (2, value)
    return (addr.version, int(addr))


def _read_head(f):
    """Return the first line of an open file, used to detect truncation."""
    f.seek(0)
    return f.readline(4096)


class LogStore:
    """Keeps records in memory and scans the detailed log incrementally.

    On startup (empty memory) it does a full read of all access_detailed.log
    files. Afterwards it only reads new bytes of each file (tracked by inode,
    size, first-line fingerprint and pending bytes). NPMplus logrotate uses
    copytruncate, so the active file is truncated in place and its previous
    content appears in a new .1 file; the dedup key avoids inserting those
    lines twice.
    """

    def __init__(self, log_dir, max_entries=300_000):
        self.log_dir = log_dir
        self.max_entries = max_entries
        self._lock = threading.RLock()
        self.records = {}
        self.order = deque()
        self.c_host = Counter()
        self.c_status = Counter()
        self.c_method = Counter()
        self.c_country = Counter()
        self.state = {}
        self.last_scan = None
        self.files_info = []
        self.parse_errors = 0
        self._logged_errors = 0

    # ---- scanning ------------------------------------------------------------
    def files(self):
        if not os.path.isdir(self.log_dir):
            return []
        try:
            names = os.listdir(self.log_dir)
        except OSError:
            return []
        out = []
        for name in names:
            if not name.startswith(LOG_PREFIX) or name.endswith(".gz"):
                continue
            full = os.path.join(self.log_dir, name)
            if os.path.isfile(full):
                out.append((name, full))

        def sort_key(item):
            name = item[0]
            if name == LOG_PREFIX:
                return (1, 0)
            suffix = name[len(LOG_PREFIX):].lstrip(".")
            try:
                return (0, -int(suffix))
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
            current = set()
            for name, full in self.files():
                try:
                    f = open(full, "rb")
                except OSError:
                    continue
                with f:
                    try:
                        st = os.fstat(f.fileno())
                    except OSError:
                        continue
                    inode = st.st_ino
                    size = st.st_size
                    current.add(inode)
                    head = _read_head(f)
                    prev = self.state.get(inode)
                    if (prev is not None and prev["size"] == size
                            and prev.get("head") == head):
                        info.append({"file": name, "inode": inode, "size": size})
                        continue
                    truncated = prev is not None and (
                        size < prev["size"] or prev.get("head") != head
                    )
                    if prev is None or truncated:
                        offset = 0
                        pending = b""
                    else:
                        offset = prev["size"]
                        pending = prev.get("pending", b"")
                    try:
                        f.seek(offset)
                        data = f.read()
                    except OSError:
                        continue
                    processed = offset + len(data)
                    chunks = (pending + data).split(b"\n")
                    next_pending = chunks.pop()
                    for raw in chunks:
                        line = raw.decode("utf-8", "replace").rstrip("\r")
                        if not line:
                            continue
                        rec = parse_line(line)
                        if rec:
                            self._add(rec)
                        elif line.startswith("["):
                            self.parse_errors += 1
                            if self._logged_errors < 5:
                                logger.warning(
                                    "Unparsed line in %s: %s", name, line[:300]
                                )
                                self._logged_errors += 1
                    self.state[inode] = {
                        "file": name,
                        "size": processed,
                        "head": head,
                        "pending": next_pending,
                    }
                    info.append({"file": name, "inode": inode, "size": size})
            self.state = {i: s for i, s in self.state.items() if i in current}
            self.files_info = info
            self.last_scan = time.time()

    # ---- insert / dedup -----------------------------------------------------
    def _add(self, rec):
        key = _key(rec)
        if key in self.records:
            return
        self.records[key] = rec
        self.order.append(key)
        self._count(rec, 1)
        while len(self.order) > self.max_entries:
            old = self.order.popleft()
            gone = self.records.pop(old, None)
            if gone:
                self._count(gone, -1)

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
        with self._lock:
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
                "parse_errors": self.parse_errors,
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
        with self._lock:
            for key in self.order:
                r = self.records[key]
                if host and r["host"] not in host:
                    continue
                if status:
                    ok = False
                    for s in status:
                        if len(s) == 3 and s.endswith("xx") and s[0].isdigit():
                            base = int(s[0]) * 100
                            if base <= r["status"] < base + 100:
                                ok = True
                                break
                        else:
                            try:
                                if r["status"] == int(s):
                                    ok = True
                                    break
                            except ValueError:
                                continue
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

        reverse = order == "desc"
        if sort == "host":
            items.sort(key=lambda r: (r["host"] or "").lower(), reverse=reverse)
        elif sort == "ip":
            items.sort(key=lambda r: _ip_sort_key(r["ip"]), reverse=reverse)
        elif sort == "method":
            items.sort(key=lambda r: (r["method"] or "").lower(), reverse=reverse)
        elif sort == "path":
            items.sort(key=lambda r: (r["path"] or "").lower(), reverse=reverse)
        elif sort == "status":
            items.sort(key=lambda r: r["status"], reverse=reverse)
        elif sort == "country":
            items.sort(key=lambda r: (r["country"] or "").lower(), reverse=reverse)
        elif sort == "time":
            items.sort(key=lambda r: r["time_ms"] if r["time_ms"] is not None else -1,
                       reverse=reverse)
        elif sort == "bytes":
            items.sort(key=lambda r: r["bytes"] if r["bytes"] is not None else r["body"],
                       reverse=reverse)
        elif sort == "body":
            items.sort(key=lambda r: r["body"], reverse=reverse)
        elif sort == "ua":
            items.sort(key=lambda r: (r["ua"] or "").lower(), reverse=reverse)
        else:
            items.sort(key=lambda r: r["epoch"], reverse=reverse)

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
        items.sort(key=lambda r: r["epoch"])
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
        if ":" in ip:
            variants = [ip, f"[{ip}]:80", f"[{ip}]:443"]
        else:
            variants = [ip, f"{ip}:80", f"{ip}:443"]
        with self._lock:
            return [{"value": v, "count": self.c_host.get(v, 0)}
                    for v in variants]

    def detect_public_ip_from_logs(self):
        """Best guess of the server's current public IP.

        Looks for a public IP in the most recent Host values, either alone or
        with an explicit :80/:443 port. Returns the most recent match.
        """
        seen = set()
        with self._lock:
            for key in reversed(self.order):
                host = self.records[key]["host"] or ""
                if not host or host in seen:
                    continue
                seen.add(host)
                candidates = [host]
                if host.startswith("["):
                    candidates.append(host.split("]")[0][1:])
                elif host.count(":") == 1:
                    h, _, p = host.rpartition(":")
                    if p in ("80", "443"):
                        candidates.append(h)
                for cand in candidates:
                    try:
                        addr = ipaddress.ip_address(cand)
                    except ValueError:
                        continue
                    if (addr.is_private or addr.is_loopback or addr.is_link_local
                            or addr.is_multicast or addr.is_unspecified
                            or addr.is_reserved):
                        continue
                    return cand
        return ""
