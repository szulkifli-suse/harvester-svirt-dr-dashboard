import datetime as dt
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, NewConnectionError

from .harvester import ping as https_ping

CACHE_SECONDS = 5
EGRESS_CACHE_SECONDS = 600


def _where(e: Exception) -> str:
    candidates = [getattr(e, "conn", None), getattr(e, "pool", None), *(e.args[:1])]
    conn = next((c for c in candidates if getattr(c, "host", None)), None)
    return f"{conn.host}:{conn.port}" if conn else "the API"


def friendly_error(e: Exception) -> str:
    if isinstance(e, MaxRetryError):
        where = _where(e)
        e = e.reason or e
    else:
        where = _where(e)
    # NewConnectionError subclasses ConnectTimeoutError, so check it first.
    if isinstance(e, NewConnectionError):
        return f"cannot connect to {where} (connection refused or no route)"
    if isinstance(e, ConnectTimeoutError):
        return f"cannot connect to {where} (timed out; check the security group or that the API is up)"
    if isinstance(e, ApiException):
        return f"Kubernetes API returned {e.status} {e.reason}"
    return str(e) or e.__class__.__name__


def _call(fn):
    try:
        return {"ok": True, "data": fn()}
    except Exception as e:  # noqa: BLE001 - each panel reports its own error
        return {"ok": False, "error": friendly_error(e)}


def summarize(host: dict, ping: dict | None, nodes: dict | None, domains: dict | None) -> dict:
    if not host["ok"]:
        return {"level": "down", "text": "EC2 host unknown"}
    state = host["data"]["state"]
    if state == "stopped":
        return {"level": "down", "text": "Stopped"}
    if state != "running":
        return {"level": "warn", "text": f"EC2 {state}"}
    if domains and domains["ok"]:
        off = [d["name"] for d in domains["data"] if d["state"] != "running"]
        if off:
            return {"level": "warn", "text": f"KVM node down: {', '.join(off)}"}
    if not (ping and ping["data"]):
        return {"level": "warn", "text": "Harvester not answering"}
    if nodes and nodes["ok"]:
        ready = sum(n["ready"] for n in nodes["data"])
        total = len(nodes["data"])
        if total and ready == total:
            return {"level": "ok", "text": "Healthy"}
        return {"level": "warn", "text": f"{ready}/{total} nodes Ready"}
    return {"level": "warn", "text": "Harvester API unreachable"}


class StatusCollector:
    def __init__(self, host, ssh, harvester, ping=https_ping):
        self.host = host
        self.ssh = ssh
        self.harvester = harvester
        self.ping = ping
        self._lock = threading.Lock()
        self._cached: tuple[float, dict] | None = None
        self._egress: tuple[float, str | None] | None = None
        self._pool = ThreadPoolExecutor(max_workers=8)

    def egress_ip(self) -> str | None:
        if self._egress and time.monotonic() - self._egress[0] < EGRESS_CACHE_SECONDS:
            return self._egress[1]
        try:
            with urllib.request.urlopen("https://ipv4.icanhazip.com/", timeout=5) as r:
                ip = r.read(64).decode().strip()
        except Exception:  # noqa: BLE001
            ip = None
        self._egress = (time.monotonic(), ip)
        return ip

    def snapshot(self) -> dict:
        with self._lock:
            if self._cached and time.monotonic() - self._cached[0] < CACHE_SECONDS:
                return self._cached[1]
            result = self._collect()
            self._cached = (time.monotonic(), result)
            return result

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None

    def _collect(self) -> dict:
        host = _call(self.host.instance)
        out = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "host": host,
               "cpu": None, "ping": None, "domains": None, "nodes": None, "vms": None, "volumes": None, "version": None}
        if host["ok"]:
            iid = host["data"]["id"]
            out["cpu"] = self._pool.submit(_call, lambda: self.host.cpu_history(iid))
            ip = host["data"]["public_ip"]
            if host["data"]["state"] == "running" and ip:
                out["ping"] = self._pool.submit(_call, lambda: self.ping(ip))
                out["domains"] = self._pool.submit(_call, lambda: self.ssh.domains(ip))
                out["nodes"] = self._pool.submit(_call, self.harvester.nodes)
                out["vms"] = self._pool.submit(_call, self.harvester.vms)
                out["volumes"] = self._pool.submit(_call, self.harvester.volumes)
                out["version"] = self._pool.submit(_call, self.harvester.version)
        for key, value in out.items():
            if hasattr(value, "result"):
                out[key] = value.result()
        out["summary"] = summarize(host, out["ping"], out["nodes"], out["domains"])
        if out["domains"] and not out["domains"]["ok"]:
            out["egress_ip"] = self.egress_ip()
        return out
