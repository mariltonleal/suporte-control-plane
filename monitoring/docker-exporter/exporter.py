#!/usr/bin/env python3
"""Exporter de containers Docker compativel com os nomes de metricas do cAdvisor.

Por que existe: o cAdvisor nao consegue monitorar containers quando o Docker usa o snapshotter do
containerd (caso do hub: "failed to identify the read-write layer ID"). Este exporter le a API do
Docker (socket) e publica, com os MESMOS nomes/labels que os dashboards e o control plane usam:

  container_cpu_usage_seconds_total, container_memory_working_set_bytes, container_memory_usage_bytes,
  container_spec_memory_limit_bytes, container_network_receive_bytes_total,
  container_network_transmit_bytes_total, container_start_time_seconds, container_restart_count,
  container_last_seen

Labels: id, name, image e container_label_<label do container> (pontos/tracos viram `_`, como no cAdvisor).
Nada de audio/video: so contadores de recursos. Sem dependencias externas (stdlib).
"""
import http.client
import json
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
API = os.environ.get("DOCKER_API_VERSION", "v1.44")
PORT = int(os.environ.get("PORT", "8080"))
WORKERS = int(os.environ.get("WORKERS", "16"))
LABEL_RE = re.compile(r"[^a-zA-Z0-9_]")


class UnixConn(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("docker", timeout=10)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(SOCKET)


def docker_get(path):
    conn = UnixConn()
    try:
        conn.request("GET", f"/{API}{path}")
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"{path}: HTTP {resp.status}")
        return json.loads(body)
    finally:
        conn.close()


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def labels_for(c, inspect):
    labels = {"id": c["Id"], "name": (c["Names"][0] if c.get("Names") else "").lstrip("/"), "image": c.get("Image", "")}
    for k, v in (c.get("Labels") or {}).items():
        labels["container_label_" + LABEL_RE.sub("_", k)] = v
    return labels


def parse_time(value):
    # 2026-09-19T13:30:17.718650123Z -> epoch
    value = value.split(".")[0].rstrip("Z")
    return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%S")) - time.timezone


def collect_one(c):
    cid = c["Id"]
    stats = docker_get(f"/containers/{cid}/stats?stream=false&one-shot=true")
    inspect = docker_get(f"/containers/{cid}/json")
    lab = labels_for(c, inspect)
    cpu = stats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0) / 1e9
    mem = stats.get("memory_stats", {}) or {}
    usage = mem.get("usage", 0) or 0
    inactive = (mem.get("stats") or {}).get("inactive_file", 0) or (mem.get("stats") or {}).get("total_inactive_file", 0) or 0
    working = max(0, usage - inactive)
    limit = mem.get("limit", 0) or 0
    rx = tx = 0
    for net in (stats.get("networks") or {}).values():
        rx += net.get("rx_bytes", 0)
        tx += net.get("tx_bytes", 0)
    state = inspect.get("State", {})
    started = parse_time(state.get("StartedAt", "1970-01-01T00:00:00Z")) if state.get("StartedAt", "").startswith("2") else 0
    return lab, {
        "container_cpu_usage_seconds_total": cpu,
        "container_memory_working_set_bytes": working,
        "container_memory_usage_bytes": usage,
        "container_spec_memory_limit_bytes": limit,
        "container_network_receive_bytes_total": rx,
        "container_network_transmit_bytes_total": tx,
        "container_start_time_seconds": started,
        "container_restart_count": inspect.get("RestartCount", 0),
        "container_last_seen": time.time(),
    }


TYPES = {
    "container_cpu_usage_seconds_total": "counter", "container_memory_working_set_bytes": "gauge",
    "container_memory_usage_bytes": "gauge", "container_spec_memory_limit_bytes": "gauge",
    "container_network_receive_bytes_total": "counter", "container_network_transmit_bytes_total": "counter",
    "container_start_time_seconds": "gauge", "container_restart_count": "gauge", "container_last_seen": "gauge",
}


def render():
    started = time.perf_counter()
    containers = docker_get("/containers/json")
    rows, errors = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for result in pool.map(lambda c: _safe(c), containers):
            if result is None:
                errors += 1
            else:
                rows.append(result)
    out = []
    for metric, kind in TYPES.items():
        out.append(f"# TYPE {metric} {kind}")
        for lab, values in rows:
            label_text = ",".join(f'{k}="{esc(v)}"' for k, v in lab.items())
            out.append(f"{metric}{{{label_text}}} {values[metric]}")
    out += [
        "# TYPE docker_exporter_containers_running gauge", f"docker_exporter_containers_running {len(containers)}",
        "# TYPE docker_exporter_errors gauge", f"docker_exporter_errors {errors}",
        "# TYPE docker_exporter_scrape_duration_seconds gauge",
        f"docker_exporter_scrape_duration_seconds {time.perf_counter() - started:.3f}",
    ]
    return "\n".join(out) + "\n"


def _safe(c):
    try:
        return collect_one(c)
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/metrics"):
            try:
                body = render().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            except Exception as exc:  # docker indisponivel
                body = f"docker_exporter_up 0\n# {exc}\n".encode()
                self.send_response(500)
        elif self.path == "/healthz":
            body = b"ok"
            self.send_response(200)
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silencioso
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
