import ipaddress
import json
import os
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .parser import LOCAL_TZ, LogStore

LOG_DIR = os.environ.get("LOG_DIR", "/logs")
REFRESH_SECONDS = float(os.environ.get("REFRESH_SECONDS", "5"))
PREF_FILE = os.environ.get("PREF_FILE", "/state/prefs.json")

# Hosts to show as chips in the "Requests" card (comma separated). Empty = top 6.
CARDS_HOSTS = [h.strip() for h in os.environ.get("CARDS_HOSTS", "").split(",") if h.strip()]

# Server public IP override. Empty = resolved from an external "what is my IP"
# API, so it follows dynamic public IPs automatically.
MY_IP = os.environ.get("MY_IP", "").strip()

# How often (seconds) to re-check the public IP with an external API.
PUBLIC_IP_REFRESH = float(os.environ.get("PUBLIC_IP_REFRESH", "600"))

_public_ip_cache = ""

_IP_APIS = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://ipinfo.io/ip",
]


def _fetch_public_ip():
    """Query external APIs for the server's outbound public IP."""
    for url in _IP_APIS:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                ip = resp.read().decode("utf-8", "replace").strip()
                if _is_public_ip(ip):
                    return ip
        except Exception:
            continue
    return None


def _fetch_home_location(ip):
    """Resolve the public IP to a city/country + coordinates via ipwho.is."""
    if not _is_public_ip(ip):
        return None
    try:
        url = f"https://ipwho.is/{ip}"
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if data.get("success"):
            return {
                "lat": data.get("latitude"),
                "lon": data.get("longitude"),
                "cc": data.get("country_code", ""),
                "city": data.get("city", ""),
            }
    except Exception:
        pass
    return None


_home_cache = {}


def _public_ip_loop():
    global _public_ip_cache, _home_cache
    while True:
        try:
            ip = _fetch_public_ip()
            if ip:
                _public_ip_cache = ip
                if _home_cache.get("ip") != ip:
                    loc = _fetch_home_location(ip)
                    if loc:
                        _home_cache = {"ip": ip, **loc}
        except Exception:
            pass
        time.sleep(PUBLIC_IP_REFRESH)

DEFAULT_PREFS = {
    "host": [],
    "status": [],
    "method": [],
    "country": [],
    "ip": "",
    "q": "",
    "hide_uptime": False,
    "hide_local": False,
    "from": "",
    "to": "",
    "page_size": 50,
    "apply_filters": True,
    "simplified": False,
}

store = LogStore(LOG_DIR)
app = FastAPI(title="NPMplus log viewer", docs_url=None, redoc_url=None)

STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _load_prefs():
    try:
        with open(PREF_FILE, "r") as f:
            data = json.load(f)
        prefs = {k: data.get(k, v) for k, v in DEFAULT_PREFS.items()}
        for k in ("host", "status", "method", "country"):
            if not isinstance(prefs[k], list):
                prefs[k] = []
        return prefs
    except (FileNotFoundError, ValueError):
        return dict(DEFAULT_PREFS)


def _save_prefs(prefs):
    try:
        os.makedirs(os.path.dirname(PREF_FILE), exist_ok=True)
        tmp = PREF_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(prefs, f)
        os.replace(tmp, PREF_FILE)
    except OSError:
        pass


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def _epoch(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.timestamp()


def _client_ip(request: Request):
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    xr = request.headers.get("x-real-ip")
    if xr:
        return xr.strip()
    return request.client.host if request.client else ""


def _is_public_ip(s):
    try:
        addr = ipaddress.ip_address(s)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_unspecified or addr.is_reserved)


def _strip_port(host):
    host = host.strip()
    if host.startswith("["):  # [ipv6]:port
        return host.split("]")[0][1:]
    if host.count(":") == 1:
        head, _, port = host.rpartition(":")
        if port.isdigit():
            return head
    return host


def _public_ip(request: Request):
    """Resolve the server public IP.

    Priority: MY_IP env, then the IP reported by an external "what is my IP"
    API (cached, refreshed periodically), then the IP seen in the logs, then
    the request Host header, then forwarded client IPs.
    """
    if MY_IP:
        return MY_IP
    if _public_ip_cache:
        return _public_ip_cache
    ip = store.detect_public_ip_from_logs()
    if ip:
        return ip
    host_hdr = request.headers.get("host", "")
    if host_hdr:
        cand = _strip_port(host_hdr)
        if _is_public_ip(cand):
            return cand
    cand = _client_ip(request)
    if _is_public_ip(cand):
        return cand
    return ""


@app.get("/api/stats")
def api_stats(request: Request):
    stats = store.stats()
    stats["cards_hosts"] = CARDS_HOSTS
    stats["my_hosts"] = store.hosts_for_public_ip(_public_ip(request))
    return stats


@app.get("/api/home")
def api_home(request: Request):
    global _home_cache
    if not _home_cache:
        ip = _public_ip(request)
        if ip:
            loc = _fetch_home_location(ip)
            if loc:
                _home_cache = {"ip": ip, **loc}
    return {
        "lat": _home_cache.get("lat"),
        "lon": _home_cache.get("lon"),
        "cc": _home_cache.get("cc", ""),
        "city": _home_cache.get("city", ""),
    }


@app.get("/api/prefs")
def api_prefs_get():
    return _load_prefs()


@app.post("/api/prefs")
async def api_prefs_set(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    prefs = _load_prefs()
    for k in DEFAULT_PREFS:
        if k in data:
            prefs[k] = data[k]
    for k in ("host", "status", "method", "country"):
        if not isinstance(prefs.get(k), list):
            prefs[k] = []
    _save_prefs(prefs)
    return prefs


def _lst(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


@app.get("/api/entries")
def api_entries(
    host: str = "",
    status: str = "",
    method: str = "",
    country: str = "",
    ip: str = "",
    q: str = "",
    exclude_ua: str = "",
    exclude_local: bool = False,
    from_: str = Query("", alias="from"),
    to: str = Query("", alias="to"),
    sort: str = "ts",
    order: str = "desc",
    collapse: bool = False,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
):
    kw = dict(
        host=_lst(host),
        status=_lst(status),
        method=_lst(method),
        country=_lst(country),
        ip=ip or None,
        q=q or None,
        exclude_ua=_lst(exclude_ua),
        exclude_local=exclude_local,
        from_epoch=_epoch(from_),
        to_epoch=_epoch(to),
    )
    return store.query(
        sort=sort,
        order=order,
        page=page,
        size=size,
        collapse=collapse,
        **kw,
    )


@app.get("/api/live")
def api_live(
    after: float = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    host: str = "",
    status: str = "",
    method: str = "",
    country: str = "",
    ip: str = "",
    q: str = "",
    exclude_ua: str = "",
    exclude_local: bool = False,
    from_: str = Query("", alias="from"),
    to: str = Query("", alias="to"),
    collapse: bool = False,
):
    items = store.query_live(
        after_epoch=after or None,
        limit=limit,
        host=_lst(host),
        status=_lst(status),
        method=_lst(method),
        country=_lst(country),
        ip=ip or None,
        q=q or None,
        exclude_ua=_lst(exclude_ua),
        exclude_local=exclude_local,
        from_epoch=_epoch(from_),
        to_epoch=_epoch(to),
        collapse=collapse,
    )
    return {"items": items}


@app.post("/api/reload")
def api_reload():
    store.scan()
    return {"ok": True, "last_scan": store.last_scan}


def _loop():
    while True:
        try:
            store.scan()
        except Exception:
            pass
        time.sleep(REFRESH_SECONDS)


store.scan()
threading.Thread(target=_loop, daemon=True).start()
threading.Thread(target=_public_ip_loop, daemon=True).start()
