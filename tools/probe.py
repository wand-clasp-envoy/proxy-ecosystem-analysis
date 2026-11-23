#!/usr/bin/env python
"""Active probing: TCP reachability, GeoIP, and real delay/throughput via mihomo.

Three independent stages, cheap to expensive:

  1. tcp     - connect server:port in parallel. Catches dead endpoints and DNS
               failures without understanding the protocol. ~2k nodes/minute.
  2. geo     - resolve the endpoint and look up a country in a local DB-IP lite
               mmdb (CC BY 4.0). Fills the region for the ~35% of nodes whose
               display name carries no usable geo label.
  3. mihomo  - load the survivors into one mihomo instance and ask its REST API
               for a per-proxy delay (real round trip through the protocol).
               Optionally select each of the fastest K in turn and download a
               2 MB payload through the HTTP inbound to measure throughput.

Standalone use is supported, but organize.py drives it via --probe/--probe-limit.

  python tools/probe.py --input data/organised.yaml --limit 200 --binary /tmp/mihomo
"""
import argparse, json, os, shutil, socket, subprocess, sys, tempfile, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
GEO_DB = os.environ.get("GEOIP_DB") or os.path.join(DATA, "geoip.mmdb")
TEST_URL = "http://cp.cloudflare.com/generate_204"
SPEED_URL = "http://speed.cloudflare.com/__down?bytes=2000000"


# ---------------------------------------------------------------- stage 1: tcp
def tcp_check(server, port, timeout=2.5):
    server, port = str(server or ""), int(port or 0)
    if not server or not port:
        return None
    t0 = time.monotonic()
    try:
        infos = socket.getaddrinfo(server, port, proto=socket.IPPROTO_TCP)
        af, socktype, proto, _, addr = infos[0]
    except Exception:
        return None
    s = socket.socket(af, socktype, proto)
    s.settimeout(timeout)
    try:
        s.connect(addr)
        return round((time.monotonic() - t0) * 1000)
    except Exception:
        return None
    finally:
        s.close()


def stage_tcp(recs, workers=128, timeout=2.5):
    def one(r):
        return r, tcp_check(r["p"].get("server"), r["p"].get("port"), timeout)

    alive = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r, ms in ex.map(one, recs):
            r["tcp_ms"] = ms
            if ms is None:
                r["alive"] = False
            else:
                r["alive"] = True
                alive += 1
    print(f"  tcp: {alive}/{len(recs)} reachable")
    return [r for r in recs if r.get("alive")]


# ---------------------------------------------------------------- stage 2: geo
def stage_geo(recs):
    if not os.path.exists(GEO_DB):
        print(f"  geo: skipped (no mmdb at {GEO_DB})")
        return
    try:
        import maxminddb
    except ImportError:
        print("  geo: skipped (pip install maxminddb)")
        return
    db = maxminddb.open_database(GEO_DB)
    cache, filled = {}, 0

    def lookup(server):
        if server in cache:
            return cache[server]
        ip = server
        try:
            ip = socket.gethostbyname(server)
        except Exception:
            cache[server] = None
            return None
        try:
            rec = db.get(ip) or {}
            cache[server] = (rec.get("country") or {}).get("iso_code")
        except Exception:
            cache[server] = None
        return cache[server]

    for r in recs:
        if r["md"].get("region"):
            continue
        cc = lookup(str(r["p"].get("server")))
        if cc:
            r["md"]["region"], r["md"]["region_source"] = cc, "geoip"
            filled += 1
    print(f"  geo: filled {filled} regions from {len(cache)} endpoints")


# -------------------------------------------------------- stage 3: mihomo probe
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Mihomo:
    def __init__(self, binary, proxies, workdir):
        self.binary = binary
        self.dir = workdir
        self.mixed, self.ctl = free_port(), free_port()
        cfg = {
            "mixed-port": self.mixed,
            "external-controller": f"127.0.0.1:{self.ctl}",
            "secret": "probe", "mode": "rule", "log-level": "silent",
            "ipv6": False, "proxies": proxies,
            "proxy-groups": [{"name": "PROBE", "type": "select",
                              "proxies": [p["name"] for p in proxies]}],
            "rules": ["MATCH,PROBE"],
        }
        self.cfg_path = os.path.join(workdir, "probe.yaml")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        self.proc = subprocess.Popen(
            [binary, "-f", self.cfg_path, "-d", workdir],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_ready(self, timeout=15):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                self.api("GET", "/version")
                return True
            except Exception:
                if self.proc.poll() is not None:
                    return False
                time.sleep(0.3)
        return False

    def api(self, method, path, body=None, timeout=35):
        url = f"http://127.0.0.1:{self.ctl}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer probe", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def delay(self, name, timeout_ms=3000):
        q = urllib.parse.urlencode({"timeout": timeout_ms, "url": TEST_URL})
        p = "/proxies/" + urllib.parse.quote(name, safe="") + "/delay?" + q
        try:
            return self.api("GET", p, timeout=timeout_ms / 1000 + 8).get("delay")
        except Exception:
            return None

    def select(self, name):
        self.api("PUT", "/proxies/PROBE", {"name": name}, timeout=10)

    def speed(self, name, limit_bytes=2_000_000, timeout=20):
        """Throughput in MB/s through the node, using the mixed-port inbound."""
        try:
            self.select(name)
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{self.mixed}"}))
            req = urllib.request.Request(SPEED_URL, headers={"User-Agent": "probe"})
            t0 = time.monotonic()
            with opener.open(req, timeout=timeout) as resp:
                n = len(resp.read(limit_bytes))
            dt = max(0.05, time.monotonic() - t0)
            return round(n / dt / 1e6, 2)
        except Exception:
            return None

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def unique_names(recs):
    seen, out = set(), []
    for i, r in enumerate(recs):
        name = str(r["p"].get("name") or f"node-{i}")
        while name in seen:
            name = f"{name}~"
        seen.add(name)
        r["p"]["name"] = name
        out.append(r)
    return out


def stage_mihomo(recs, binary, chunk=1200, workers=24, timeout_ms=3000,
                 speed_top=0, speed_bytes=2_000_000):
    if not binary or not os.path.exists(binary):
        print(f"  mihomo: skipped (binary not found: {binary})")
        return recs
    recs = unique_names(recs)
    alive = []
    for start in range(0, len(recs), chunk):
        part = recs[start:start + chunk]
        tmp = tempfile.mkdtemp(prefix="probe-")
        inst = None
        try:
            inst = Mihomo(binary, [r["p"] for r in part], tmp)
            if not inst.wait_ready():
                print(f"  mihomo: failed to start (chunk {start}); keeping tcp result")
                alive.extend(part)
                continue
            with ThreadPoolExecutor(max_workers=workers) as ex:
                delays = list(ex.map(lambda r: inst.delay(r["p"]["name"], timeout_ms), part))
            for r, d in zip(part, delays):
                r["delay_ms"] = d
                r["alive"] = d is not None
                if d is not None:
                    alive.append(r)
            print(f"  mihomo: chunk {start//chunk + 1} -> "
                  f"{sum(1 for d in delays if d is not None)}/{len(part)} alive")
        finally:
            if inst:
                inst.close()
            shutil.rmtree(tmp, ignore_errors=True)

    if speed_top and alive:
        top = sorted(alive, key=lambda r: r.get("delay_ms") or 9999)[:speed_top]
        tmp = tempfile.mkdtemp(prefix="speed-")
        inst = None
        try:
            inst = Mihomo(binary, [r["p"] for r in top], tmp)
            if inst.wait_ready():
                for i, r in enumerate(top, 1):
                    r["speed_mbps"] = inst.speed(r["p"]["name"], speed_bytes)
                    if i % 25 == 0:
                        print(f"  speed: {i}/{len(top)}")
        finally:
            if inst:
                inst.close()
            shutil.rmtree(tmp, ignore_errors=True)
    return alive


# ---------------------------------------------------------------- entry point
def probe(recs, binary=None, tcp_timeout=2.5, workers=128, chunk=1200,
          delay_workers=24, speed_top=0, geo=True, verbose=True):
    """Probe `recs` (list of {"p": proxy, "md": meta}) in place; returns survivors."""
    if not recs:
        return recs
    if verbose:
        print(f"probing {len(recs)} nodes")
    if geo:
        stage_geo(recs)          # cheap and independent of reachability
    recs = stage_tcp(recs, workers=workers, timeout=tcp_timeout)
    if binary:
        recs = stage_mihomo(recs, binary, chunk=chunk, workers=delay_workers,
                            speed_top=speed_top)
    return recs


def report(recs):
    lat = sorted(r["tcp_ms"] for r in recs if r.get("tcp_ms"))
    dly = sorted(r["delay_ms"] for r in recs if r.get("delay_ms"))
    spd = sorted(r["speed_mbps"] for r in recs if r.get("speed_mbps"))
    def q(xs, p):
        return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1) if xs else None
    return {"alive": len(recs), "tcp_median_ms": q(lat, .5),
            "delay_median_ms": q(dly, .5), "speed_median": q(spd, .5),
            "speed_p90": q(spd, .9),
            "regions_from_geoip": sum(1 for r in recs
                                      if r.get("md", {}).get("region_source") == "geoip")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=os.path.join(DATA, "organised.yaml"))
    ap.add_argument("--output", default=os.path.join(DATA, "probed.yaml"))
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--binary", default=os.environ.get("MIHOMO_BIN"))
    ap.add_argument("--speed-top", type=int, default=0)
    a = ap.parse_args()

    with open(a.input, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    proxies = [p for p in (doc.get("proxies") or []) if isinstance(p, dict)][:a.limit]
    recs = [{"p": dict(p), "md": {}} for p in proxies]
    recs = probe(recs, binary=a.binary, speed_top=a.speed_top)
    print(json.dumps(report(recs), ensure_ascii=False))
    doc["proxies"] = [r["p"] for r in recs]
    with open(a.output, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False, width=4096)
    print(f"wrote {a.output} ({len(recs)} nodes)")


if __name__ == "__main__":
    main()
