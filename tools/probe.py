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
import argparse, base64, json, os, re, shutil, socket, subprocess, sys, tempfile, time
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
    print(f"  tcp: {alive}/{len(recs)} reachable", flush=True)
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
    print(f"  geo: filled {filled} regions from {len(cache)} endpoints", flush=True)


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
        self.log_path = os.path.join(workdir, "mihomo.log")
        self.log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [binary, "-f", self.cfg_path, "-d", workdir],
            stdout=self.log, stderr=self.log)

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

    def speed(self, name, limit_bytes=2_000_000, timeout=12):
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

    def why(self):
        try:
            self.log.flush()
            with open(self.log_path, "rb") as f:
                lines = f.read().decode("utf-8", "ignore").strip().splitlines()
            return " | ".join(lines[-2:])[:300]
        except Exception:
            return ""

    def close(self):
        try:
            self.log.close()
        except Exception:
            pass
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


BAD_TYPES = {"", None}


def _valid_reality(p):
    """mihomo aborts the whole config on REALITY args it cannot parse.

    Its rules are stricter than the spec: short-id must be empty or exactly 8
    bytes (16 hex chars), and public-key must be 32 bytes of base64(url).
    Anything else is dropped from the probe batch (kept as unverified).
    """
    ro = p.get("reality-opts")
    if not isinstance(ro, dict):
        return True
    sid = str(ro.get("short-id") or "")
    if sid and not re.fullmatch(r"[0-9a-fA-F]{16}", sid):
        return False
    pk = str(ro.get("public-key") or "")
    if not pk:
        return False
    try:
        raw = base64.urlsafe_b64decode(pk.replace("+", "-").replace("/", "_")
                                       + "=" * (-len(pk) % 4))
    except Exception:
        return False
    return len(raw) == 32


def _probe_part(part, binary, workers, timeout_ms, depth=0):
    """Probe one batch; on config failure split in half and retry.

    A single malformed proxy makes mihomo refuse the whole file, so bisecting is
    the only way to keep the rest of the batch testable.
    """
    tmp = tempfile.mkdtemp(prefix="probe-")
    inst = None
    try:
        inst = Mihomo(binary, [r["p"] for r in part], tmp)
        if not inst.wait_ready():
            why = inst.why()
            if len(part) > 64 and depth < 6:
                half = len(part) // 2
                print(f"  mihomo: config rejected ({len(part)} nodes), splitting: {why}")
                return (_probe_part(part[:half], binary, workers, timeout_ms, depth + 1) +
                        _probe_part(part[half:], binary, workers, timeout_ms, depth + 1))
            print(f"  mihomo: cannot load {len(part)} nodes, keeping tcp result: {why}")
            for r in part:
                r["alive"] = True          # tcp said yes; keep, but unverified
            return part
        with ThreadPoolExecutor(max_workers=workers) as ex:
            delays = list(ex.map(lambda r: inst.delay(r["p"]["name"], timeout_ms), part))
        out = []
        for r, d in zip(part, delays):
            r["delay_ms"] = d
            r["alive"] = d is not None
            r["verified"] = d is not None
            if d is not None:
                out.append(r)
        print(f"  mihomo: {sum(1 for d in delays if d is not None)}/{len(part)} alive", flush=True)
        return out
    finally:
        if inst:
            inst.close()
        shutil.rmtree(tmp, ignore_errors=True)


def stage_mihomo(recs, binary, chunk=1200, workers=24, timeout_ms=3000,
                 speed_top=0, speed_bytes=2_000_000, speed_budget=300):
    if not binary or not os.path.exists(binary):
        print(f"  mihomo: skipped (binary not found: {binary})")
        return recs
    usable = [r for r in recs
              if r["p"].get("type") not in BAD_TYPES and _valid_reality(r["p"])]
    skipped = len(recs) - len(usable)
    if skipped:
        print(f"  mihomo: skipped {skipped} nodes mihomo would refuse to load "
              f"(missing type / bad REALITY args)")
    recs = unique_names(usable)
    alive = []
    for start in range(0, len(recs), chunk):
        alive.extend(_probe_part(recs[start:start + chunk], binary, workers, timeout_ms))

    if speed_top and alive:
        top = sorted(alive, key=lambda r: r.get("delay_ms") or 9999)[:speed_top]
        tmp = tempfile.mkdtemp(prefix="speed-")
        inst = None
        deadline = time.monotonic() + max(0, speed_budget)
        try:
            inst = Mihomo(binary, [r["p"] for r in top], tmp)
            if inst.wait_ready():
                for i, r in enumerate(top, 1):
                    if time.monotonic() > deadline:
                        print(f"  speed: budget {speed_budget}s exhausted at {i - 1}/{len(top)}")
                        break
                    r["speed_mbps"] = inst.speed(r["p"]["name"], speed_bytes)
                    if i % 25 == 0:
                        print(f"  speed: {i}/{len(top)}", flush=True)
        finally:
            if inst:
                inst.close()
            shutil.rmtree(tmp, ignore_errors=True)
    return alive


# ---------------------------------------------------------------- entry point
def probe(recs, binary=None, tcp_timeout=2.5, workers=128, chunk=1200,
          delay_workers=24, speed_top=0, speed_budget=300, geo=True, verbose=True):
    """Probe `recs` (list of {"p": proxy, "md": meta}) in place; returns survivors."""
    if not recs:
        return recs
    if verbose:
        print(f"probing {len(recs)} nodes")
    if geo:
        stage_geo(recs)          # cheap and independent of reachability
    recs = stage_tcp(recs, workers=workers, timeout=tcp_timeout)
    if not binary:
        for r in recs:
            r["verified"] = True     # tcp-only mode: reachability is all we have
    if binary:
        recs = stage_mihomo(recs, binary, chunk=chunk, workers=delay_workers,
                            speed_top=speed_top, speed_budget=speed_budget)
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


# ------------------------------------------------------------- result lifecycle
def load_cache(path=None):
    """fp -> {last_ok, last_probe, miss, delay_ms, speed_mbps, region}."""
    path = path or os.path.join(DATA, "probe_cache.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache, path=None):
    path = path or os.path.join(DATA, "probe_cache.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))


def _age_min(iso, now):
    import datetime as dt
    if not iso:
        return 10 ** 9
    return (now - dt.datetime.fromisoformat(iso)).total_seconds() / 60


def apply_cache(recs, cache, ttl_min=180, max_miss=2, now=None):
    """Sort recs into four buckets so a cycle never re-tests what it just learned.

    fresh   - never seen, or the stored result is older than ttl_min -> probe now
    reused  - probed successfully within ttl_min -> reuse stored delay/speed
    cooling - failed recently (miss < max_miss) -> skip this cycle entirely
    expired - failed max_miss cycles in a row -> drop from the pool
    """
    import datetime as dt
    now = now or dt.datetime.now(dt.timezone.utc)
    fresh, reused, cooling, expired = [], [], [], []
    for r in recs:
        c = cache.get(r.get("fp") or "")
        if not c:
            fresh.append(r)
            continue
        if c.get("miss", 0) >= max_miss:
            expired.append(r)
            continue
        recent = _age_min(c.get("last_probe"), now) <= ttl_min
        if c.get("last_ok") and _age_min(c["last_ok"], now) <= ttl_min:
            r["delay_ms"] = c.get("delay_ms")
            r["speed_mbps"] = c.get("speed_mbps")
            r["alive"] = True
            r["from_cache"] = True
            reused.append(r)
        elif recent:
            cooling.append(r)
        else:
            fresh.append(r)
    return fresh, reused, cooling, expired


def record(cache, probed, now=None):
    """Merge this cycle's results into the cache."""
    import datetime as dt
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    for r in probed:
        fp = r.get("fp")
        if not fp:
            continue
        c = cache.setdefault(fp, {})
        c["last_probe"] = stamp
        if r.get("alive") and r.get("verified", True):
            c["last_ok"] = stamp
            c["miss"] = 0
            if r.get("delay_ms"):
                c["delay_ms"] = r["delay_ms"]
            if r.get("speed_mbps"):
                c["speed_mbps"] = r["speed_mbps"]
            if r.get("md", {}).get("region_source") == "geoip":
                c["region"] = r["md"].get("region")
        else:
            c["miss"] = c.get("miss", 0) + 1
    return cache
