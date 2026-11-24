#!/usr/bin/env python
"""Build a small subscription that actually works from *this* network.

The GitHub-hosted feed is verified from Azure; from a censored/residential
network most of those endpoints are unreachable at TCP level, which looks like
"the subscription does not work". This script re-probes the published file from
the local machine and keeps only what answers here.

  python tools/local_lite.py --url <gist raw url> --keep 200
"""
import argparse, collections, json, os, re, subprocess, sys, tempfile
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe import Mihomo, tcp_check  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
FLAGS = {"HK": "🇭🇰", "TW": "🇹🇼", "JP": "🇯🇵", "SG": "🇸🇬", "KR": "🇰🇷", "US": "🇺🇸",
         "CA": "🇨🇦", "GB": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷", "NL": "🇳🇱", "RU": "🇷🇺"}


def region(name):
    s = str(name)
    m = re.match(r"^\s*([\U0001F1E6-\U0001F1FF]{2})\s*([A-Za-z]{2})", s)
    if m:
        return m.group(2).upper()
    m = re.match(r"^([A-Z]{2})[-_]", s)
    if m:
        return m.group(1)
    m = re.search(r"[\[\(【]([A-Za-z]{2})[\]\)】]", s)
    return m.group(1).upper() if m else "??"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--input", default=os.path.join(DATA, "audit-input.yaml"))
    ap.add_argument("--keep", type=int, default=200)
    ap.add_argument("--binary", default=os.environ.get("MIHOMO_BIN"))
    ap.add_argument("--speed-top", type=int, default=20)
    ap.add_argument("--reuse", action="store_true", help="reuse the cached TCP pass")
    ap.add_argument("--probe-cap", type=int, default=800,
                    help="protocol-verify at most this many lowest-TCP-latency nodes")
    ap.add_argument("--out", default=os.path.join(DATA, "local-lite.yaml"))
    a = ap.parse_args()

    if a.url:
        import urllib.request
        with urllib.request.urlopen(a.url, timeout=60) as r:
            raw = r.read().decode("utf-8")
        src = os.path.join(DATA, "audit-input.yaml")
        with open(src, "w", encoding="utf-8") as f:
            f.write(raw)
        a.input = src
    proxies = (yaml.safe_load(open(a.input, encoding="utf-8")) or {}).get("proxies") or []
    print(f"loaded {len(proxies)} proxies")

    from concurrent.futures import ThreadPoolExecutor
    cache_path = os.path.join(DATA, "lite-tcp.json")
    if a.reuse and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            alive = [(p, ms) for p, ms in json.load(f)]
        print(f"reused TCP pass: {len(alive)} alive")
    else:
        with ThreadPoolExecutor(96) as ex:
            lat = list(ex.map(lambda p: tcp_check(p.get("server"), p.get("port"), 2.5), proxies))
        alive = [(p, l) for p, l in zip(proxies, lat) if l is not None]
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(alive, f, ensure_ascii=False)
    print(f"tcp reachable here: {len(alive)}/{len(proxies)} ({100*len(alive)/max(1,len(proxies)):.1f}%)")
    if not alive or not a.binary:
        print("nothing to verify / no mihomo binary")
        return

    alive.sort(key=lambda x: x[1])          # cheapest by TCP latency first
    alive = alive[:a.probe_cap]
    print(f"protocol-verifying {len(alive)} nodes (tcp latency <= {alive[-1][1]} ms)")
    recs = [{"p": dict(p), "md": {}} for p, _ in alive]
    for i, r in enumerate(recs):
        r["p"]["name"] = f"{region(r['p']['name'])}-{i}"
    tmp = tempfile.mkdtemp(prefix="lite-")
    inst = Mihomo(a.binary, [r["p"] for r in recs], tmp)
    if not inst.wait_ready():
        print("mihomo refused the config:", inst.why())
        inst.close()
        return
    with ThreadPoolExecutor(24) as ex:
        delays = list(ex.map(lambda r: inst.delay(r["p"]["name"], 4000), recs))
    ok = [(r, d) for r, d in zip(recs, delays) if d]
    print(f"protocol-verified here: {len(ok)}/{len(recs)}")

    speeds = {}
    for r, d in sorted(ok, key=lambda x: x[1])[:a.speed_top]:
        inst.select(r["p"]["name"])
        speeds[r["p"]["name"]] = inst.speed(r["p"]["name"])
    inst.close()

    ok.sort(key=lambda x: x[1])
    keep = ok[:a.keep]
    by_reg = collections.defaultdict(list)
    for i, (r, d) in enumerate(keep, 1):
        p = dict(r["p"])
        reg = region(p["name"])
        speed = speeds.get(r["p"]["name"])
        bits = [f"{FLAGS.get(reg, '🏳️')}{reg}_{len(by_reg[reg])+1:02d}", f"{d}ms"]
        if speed:
            bits.append(f"{speed:.1f}MB/s")
        p["name"] = "|".join(bits)
        by_reg[reg].append(p)

    proxies_out = [p for reg in by_reg for p in by_reg[reg]]
    groups = [{"name": "🚀 节点选择", "type": "select",
               "proxies": ["♻️ 自动选择"] + [f"{FLAGS.get(r, '🏳️')}{r} 节点" for r in by_reg]},
              {"name": "♻️ 自动选择", "type": "url-test",
               "url": "http://cp.cloudflare.com/generate_204",
               "interval": 300, "tolerance": 50, "lazy": False,
               "proxies": [p["name"] for p in proxies_out[:100]]}]
    for reg, lst in by_reg.items():
        groups.append({"name": f"{FLAGS.get(reg, '🏳️')}{reg} 节点", "type": "url-test",
                       "url": "http://cp.cloudflare.com/generate_204",
                       "interval": 300, "tolerance": 50,
                       "proxies": [p["name"] for p in lst]})

    doc = {
        "mixed-port": 7890, "allow-lan": False, "mode": "rule", "log-level": "info",
        "unified-delay": True, "tcp-concurrent": True, "find-process-mode": "off",
        "dns": {"enable": True, "ipv6": False, "enhanced-mode": "fake-ip",
                "fake-ip-range": "198.18.0.1/16",
                "nameserver": ["https://doh.pub/dns-query", "https://dns.alidns.com/dns-query"],
                "fallback": ["https://1.1.1.1/dns-query", "https://dns.google/dns-query"]},
        "proxies": proxies_out,
        "proxy-groups": groups,
        "rules": ["MATCH,🚀 节点选择"],
    }
    with open(a.out, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False, width=4096)
    print(f"wrote {a.out}: {len(proxies_out)} proxies, {len(by_reg)} regions, "
          f"{os.path.getsize(a.out)/1024:.0f} KB")
    r = subprocess.run([a.binary, "-t", "-f", a.out, "-d", tempfile.mkdtemp()],
                       capture_output=True, text=True, timeout=120)
    print("mihomo -t:", "ok" if r.returncode == 0 else (r.stderr or r.stdout).strip()[-200:])


if __name__ == "__main__":
    main()
