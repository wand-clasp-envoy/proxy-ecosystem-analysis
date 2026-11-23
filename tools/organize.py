#!/usr/bin/env python
"""Turn a set of gist feeds into one organised subscription.

Stages (mirror what the ecosystem's publishers do, but with a stable identity):

  1. cache   - download the node-bearing yaml of every feed into data/raw/
  2. parse   - decode the display name back into structured metadata
  3. filter  - health / speed / protocol gates
  4. dedupe  - transport fingerprint, never the display name
  5. rank    - per region by (health desc, speed desc), renumber _01.._NN
  6. emit    - all.yaml (flat), organised.yaml (+groups/rules), report.json

  GH_TOKEN=$(gh auth token) python tools/organize.py --cache
  python tools/organize.py --health 20 --speed 1 --out data/organised.yaml
"""
import argparse, base64, collections, hashlib, json, os, re, statistics
import yaml
from harvest import DATA, get, load, save
from fetch_nodes import fingerprint

RAW = os.path.join(DATA, "raw")

# ISO2 -> (flag, 中文名). Only the common exits; anything else keeps its ISO2.
REGIONS = {
    "HK": ("🇭🇰", "香港"), "TW": ("🇹🇼", "台湾"), "JP": ("🇯🇵", "日本"),
    "SG": ("🇸🇬", "新加坡"), "KR": ("🇰🇷", "韩国"), "US": ("🇺🇸", "美国"),
    "CA": ("🇨🇦", "加拿大"), "GB": ("🇬🇧", "英国"), "UK": ("🇬🇧", "英国"),
    "DE": ("🇩🇪", "德国"), "FR": ("🇫🇷", "法国"), "NL": ("🇳🇱", "荷兰"),
    "IT": ("🇮🇹", "意大利"), "ES": ("🇪🇸", "西班牙"), "SE": ("🇸🇪", "瑞典"),
    "CH": ("🇨🇭", "瑞士"), "PL": ("🇵🇱", "波兰"), "RU": ("🇷🇺", "俄罗斯"),
    "AU": ("🇦🇺", "澳大利亚"), "IN": ("🇮🇳", "印度"), "TR": ("🇹🇷", "土耳其"),
    "BR": ("🇧🇷", "巴西"), "AR": ("🇦🇷", "阿根廷"), "VN": ("🇻🇳", "越南"),
    "TH": ("🇹🇭", "泰国"), "MY": ("🇲🇾", "马来西亚"), "ID": ("🇮🇩", "印尼"),
    "PH": ("🇵🇭", "菲律宾"), "AE": ("🇦🇪", "阿联酋"), "IL": ("🇮🇱", "以色列"),
    "FI": ("🇫🇮", "芬兰"), "NO": ("🇳🇴", "挪威"), "DK": ("🇩🇰", "丹麦"),
    "IE": ("🇮🇪", "爱尔兰"), "AT": ("🇦🇹", "奥地利"), "CZ": ("🇨🇿", "捷克"),
    "RO": ("🇷🇴", "罗马尼亚"), "LT": ("🇱🇹", "立陶宛"), "LV": ("🇱🇻", "拉脱维亚"),
    "UA": ("🇺🇦", "乌克兰"), "KZ": ("🇰🇿", "哈萨克"), "MO": ("🇲🇴", "澳门"),
    "MX": ("🇲🇽", "墨西哥"), "ZA": ("🇿🇦", "南非"), "CL": ("🇨🇱", "智利"),
    "CO": ("🇨🇴", "哥伦比亚"), "SA": ("🇸🇦", "沙特"), "HU": ("🇭🇺", "匈牙利"),
    "PT": ("🇵🇹", "葡萄牙"), "GR": ("🇬🇷", "希腊"), "BE": ("🇧🇪", "比利时"),
    "EE": ("🇪🇪", "爱沙尼亚"), "IS": ("🇮🇸", "冰岛"), "NZ": ("🇳🇿", "新西兰"),
}
# Chinese city / provider words -> ISO2
CN2ISO = {
    "香港": "HK", "台湾": "TW", "台北": "TW", "日本": "JP", "新加坡": "SG",
    "狮城": "SG", "美国": "US", "韩国": "KR", "德国": "DE", "英国": "GB",
    "法国": "FR", "荷兰": "NL", "加拿大": "CA", "澳大利亚": "AU", "澳洲": "AU",
    "俄罗斯": "RU", "印度": "IN", "土耳其": "TR", "巴西": "BR", "阿根廷": "AR",
    "越南": "VN", "泰国": "TH", "马来西亚": "MY", "印尼": "ID", "菲律宾": "PH",
    "阿联酋": "AE", "迪拜": "AE", "波兰": "PL", "瑞典": "SE", "瑞士": "CH",
    "芬兰": "FI", "挪威": "NO", "丹麦": "DK", "西班牙": "ES", "意大利": "IT",
    "捷克": "CZ", "奥地利": "AT", "罗马尼亚": "RO", "立陶宛": "LT",
    "拉脱维亚": "LV", "乌克兰": "UA", "哈萨克": "KZ", "澳门": "MO",
    "爱尔兰": "IE", "冰岛": "IS", "以色列": "IL", "沙特": "SA", "南非": "ZA",
    "智利": "CL", "墨西哥": "MX", "哥伦比亚": "CO", "葡萄牙": "PT", "希腊": "GR",
    "比利时": "BE", "新西兰": "NZ", "爱沙尼亚": "EE", "匈牙利": "HU",
}
CAMPUS = ("GCP", "AWS", "AZ", "Azure", "TK", "Oracle", "OCI", "DO", "Vultr",
          "Linode", "CC", "RF", "VK", "BGP", "IEPL", "IPLC", "CN2", "GIA")
TAG_DISPLAY = {"chatgpt": "GPT", "gemini": "GM", "netflix": "NF",
               "youtube": "YT", "twitter": "X", "disney": "D+",
               "tiktok": "TK", "ai": "AI"}
ISO3 = {"MYS": "MY", "USA": "US", "HKG": "HK", "JPN": "JP", "SGP": "SG",
        "DEU": "DE", "GBR": "GB", "FRA": "FR", "NLD": "NL", "KOR": "KR",
        "TWN": "TW", "ITA": "IT", "RUS": "RU", "CAN": "CA", "AUS": "AU",
        "IND": "IN", "TUR": "TR", "BRA": "BR", "ARG": "AR", "VNM": "VN",
        "THA": "TH", "IDN": "ID", "PHL": "PH", "ARE": "AE", "ISR": "IL",
        "FIN": "FI", "NOR": "NO", "DNK": "DK", "IRL": "IE", "AUT": "AT",
        "CZE": "CZ", "ROU": "RO", "LTU": "LT", "LVA": "LV", "UKR": "UA",
        "KAZ": "KZ", "MEX": "MX", "ZAF": "ZA", "CHL": "CL", "COL": "CO",
        "SAU": "SA", "HUN": "HU", "PRT": "PT", "GRC": "GR", "BEL": "BE",
        "EST": "EE", "ISL": "IS", "NZL": "NZ", "POL": "PL", "SWE": "SE",
        "CHE": "CH", "ESP": "ES"}


def unit_to_mbps(v, unit):
    """Display names use MB/s and KB/s; normalise everything to MB/s."""
    v = float(v)
    return v / 1024 if unit.upper().startswith("KB") else v


def parse_name(name):
    """Return structured metadata from a display name; tolerant of 3 naming schools."""
    name = str(name or "")
    out = {"region": None, "speed": None, "health": None, "expiry": None,
           "unlock": [], "line": None, "style": "plain"}
    # region: flag+ISO2 | [ISO2] | Chinese word | bare ISO2 token
    m = re.match(r"^\s*([\U0001F1E6-\U0001F1FF]{2})\s*([A-Za-z]{2,3})(?![A-Za-z])", name)
    if m:
        code = m.group(2).upper()
        iso = ISO3.get(code, code[:2])
        out["region"] = iso if re.fullmatch(r"[A-Z]{2}", iso) else None
        out["style"] = "flag+ISO2"
    else:
        m = re.search(r"[\[\(【]\s*([A-Za-z]{2})\s*[\]\)】]", name)
        if m and m.group(1).upper() in REGIONS:
            out["region"], out["style"] = m.group(1).upper(), "[ISO2]"
        else:
            for cn, iso in CN2ISO.items():
                if cn in name:
                    out["region"], out["style"] = iso, "中文"
                    break
    # speed / health / expiry
    sm = re.search(r"(\d+(?:\.\d+)?)\s*(KB|MB)/s", name, re.I)
    if sm:
        out["speed"] = round(unit_to_mbps(sm.group(1), sm.group(2)), 2)
    hm = re.search(r"\b(\d{1,3})%", name)
    if hm:
        out["health"] = int(hm.group(1))
    em = re.search(r"\b(?:R|P|T)?\d{2,4}[-–](2[0-9])(\d{2})(\d{2})\b", name)
    if em:
        out["expiry"] = f"20{em.group(1)}-{em.group(2)}-{em.group(3)}"
    for tag, canon in (("GPT", "chatgpt"), ("GM", "gemini"), ("NF", "netflix"),
                       ("YT", "youtube"), ("X", "twitter"), ("D+", "disney"),
                       ("AI", "ai")):
        # TK is deliberately absent: it is a line/campus tag (TK-GCP), not an unlock
        pat = rf"(^|[|\s\[]){re.escape(tag)}(?=$|[|\s\]\-⁺])"
        if re.search(pat, name, re.I):
            if canon not in out["unlock"]:
                out["unlock"].append(canon)
    for kw in CAMPUS:
        if re.search(rf"\b{kw}\b", name):
            out["line"] = kw
            break
    return out


def cache(feeds):
    os.makedirs(RAW, exist_ok=True)
    for gid, meta in feeds.items():
        p = os.path.join(RAW, f"{gid}.yaml")
        if os.path.exists(p):
            continue
        try:
            j = json.loads(get(f"https://api.github.com/gists/{gid}"))
        except Exception as e:
            print(f"  {meta['owner'][:16]:16s} ERR {e}")
            continue
        best = (None, [])
        for fname, fm in j["files"].items():
            if not re.search(r"\.ya?ml$", fname, re.I):
                continue
            try:
                d = yaml.safe_load(fm.get("content") or "")
            except Exception:
                continue
            px = [x for x in ((d or {}).get("proxies") or []) if isinstance(x, dict)] if isinstance(d, dict) else []
            if len(px) > len(best[1]):
                best = (fname, px)
        if best[1]:
            with open(p, "w", encoding="utf-8") as f:
                yaml.safe_dump({"proxies": best[1]}, f, allow_unicode=True, sort_keys=False)
            print(f"  cached {meta['owner'][:16]:16s} {best[0]:18s} n={len(best[1])}")


def load_cached(feeds):
    out = []
    for gid, meta in feeds.items():
        p = os.path.join(RAW, f"{gid}.yaml")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            px = (yaml.safe_load(f) or {}).get("proxies") or []
        for p_ in px:
            out.append((meta["owner"], gid, p_))
    return out


def organise(rows, health_min=None, speed_min=None, drop=("http", "socks5", "null")):
    seen, kept, stat = {}, [], collections.Counter()
    for owner, gid, p in rows:
        stat["in"] += 1
        fp = fingerprint(p)
        md = parse_name(p.get("name"))
        if p.get("type") in drop:
            stat["drop_type"] += 1
            continue
        if health_min is not None and md["health"] is not None and md["health"] < health_min:
            stat["drop_health"] += 1
            continue
        if speed_min is not None and md["speed"] is not None and md["speed"] < speed_min:
            stat["drop_speed"] += 1
            continue
        if fp in seen:
            stat["dup"] += 1
            if md["speed"] and md["speed"] > (seen[fp]["md"]["speed"] or 0):
                seen[fp].update(p=p, md=md, owner=owner)
            continue
        rec = {"owner": owner, "gid": gid, "p": p, "md": md, "fp": fp}
        seen[fp] = rec
        kept.append(rec)
    kept = rank_and_rename(kept)
    return kept, stat


def rank_and_rename(kept):
    """Sort each region and write the display name.

    Order: measured delay (probed), then measured throughput, then the values the
    publisher advertised. Called twice when probing - once before, once after.
    """
    by = collections.defaultdict(list)
    for r in kept:
        by[r["md"]["region"] or "??"].append(r)
    for reg, lst in by.items():
        lst.sort(key=lambda r: (r.get("delay_ms") or 10 ** 6,
                                -(r.get("speed_mbps") or 0),
                                -(r["md"]["speed"] or 0),
                                -(r["md"]["health"] or 0)))
        flag = REGIONS.get(reg, ("🏳️", "未知"))[0]
        for i, r in enumerate(lst, 1):
            bits = [f"{flag}{reg}_{i:02d}"]
            mbps = r.get("speed_mbps") or r["md"]["speed"]
            if mbps:
                bits.append(f"{mbps:.1f}MB/s")
            if r.get("delay_ms"):
                bits.append(f"{r['delay_ms']}ms")
            elif r["md"]["health"] is not None:
                bits.append(f"{r['md']['health']}%")
            if r["md"]["line"]:
                bits.append(r["md"]["line"])
            bits.extend(TAG_DISPLAY.get(t, t.upper()) for t in r["md"]["unlock"][:3])
            r["new_name"] = "|".join(bits)
    return kept


RULE_PROVIDERS = {
    "AI": ("https://testingcf.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Ruleset/OpenAi.list", "classical"),
    "YouTube": ("https://testingcf.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Ruleset/YouTube.list", "classical"),
    "Netflix": ("https://testingcf.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Ruleset/Netflix.list", "classical"),
    "Telegram": ("https://testingcf.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Ruleset/Telegram.list", "classical"),
    "ChinaDomain": ("https://testingcf.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/ChinaDomain.list", "domain"),
    "ChinaCompanyIp": ("https://testingcf.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/ChinaCompanyIp.list", "ipcidr"),
}


def emit(kept, path, with_groups=True, min_region=5, max_regions=12):
    kept = sorted(kept, key=lambda r: (r["md"]["region"] or "ZZ",
                                       r.get("delay_ms") or 10 ** 6,
                                       -(r.get("speed_mbps") or r["md"]["speed"] or 0)))
    proxies = []
    for r in kept:
        p = dict(r["p"])
        p["name"] = r["new_name"]
        proxies.append(p)
    doc = {"proxies": proxies}
    if with_groups:
        count = collections.Counter(r["md"]["region"] for r in kept)
        big = [reg for reg, c in count.most_common(max_regions) if reg and c >= min_region]
        small = [r["new_name"] for r in kept if not r["md"]["region"] or count[r["md"]["region"]] < min_region]
        selector = ["♻️ 自动选择"] + [f"{REGIONS.get(x, ('🏳️',))[0]}{x} 节点" for x in big]
        if small:
            selector.append("🌍 其他")
        groups = [{"name": "🚀 节点选择", "type": "select", "proxies": selector},
                  {"name": "♻️ 自动选择", "type": "url-test",
                   "url": "https://www.gstatic.com/generate_204", "interval": 300,
                   "tolerance": 50, "proxies": [r["new_name"] for r in kept]},
                  {"name": "💬 AI 平台", "type": "select", "proxies": ["♻️ 自动选择", "🚀 节点选择"]},
                  {"name": "📺 流媒体", "type": "select", "proxies": ["♻️ 自动选择", "🚀 节点选择"]},
                  {"name": "🎯 全球直连", "type": "select", "proxies": ["DIRECT", "🚀 节点选择"]}]
        for reg in big:
            flag, cn = REGIONS.get(reg, ("🏳️", "未知"))
            groups.append({"name": f"{flag}{reg} 节点", "type": "url-test",
                           "url": "https://www.gstatic.com/generate_204", "interval": 300,
                           "tolerance": 50,
                           "proxies": [r["new_name"] for r in kept if r["md"]["region"] == reg]})
        if small:
            groups.append({"name": "🌍 其他", "type": "url-test",
                           "url": "https://www.gstatic.com/generate_204", "interval": 300,
                           "tolerance": 50, "proxies": small})
        doc["proxy-groups"] = groups
        doc["rule-providers"] = {k: {"type": "http", "behavior": b, "url": u,
                                     "path": f"./rules/{k}.list", "interval": 86400}
                                 for k, (u, b) in RULE_PROVIDERS.items()}
        doc["rules"] = ["RULE-SET,AI,💬 AI 平台", "RULE-SET,YouTube,📺 流媒体",
                        "RULE-SET,Netflix,📺 流媒体", "RULE-SET,Telegram,🚀 节点选择",
                        "RULE-SET,ChinaDomain,🎯 全球直连", "RULE-SET,ChinaCompanyIp,🎯 全球直连",
                        "GEOIP,CN,🎯 全球直连", "MATCH,🚀 节点选择"]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False, width=4096)
    return len(proxies)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", action="store_true")
    ap.add_argument("--health", type=int, default=None)
    ap.add_argument("--speed", type=float, default=None)
    ap.add_argument("--out", default=os.path.join(DATA, "organised.yaml"))
    ap.add_argument("--max-regions", type=int, default=12,
                    help="how many regions get their own group")
    ap.add_argument("--min-region", type=int, default=5,
                    help="regions with fewer nodes go into a single 🌍 其他 group")
    ap.add_argument("--flat", default=os.path.join(DATA, "organized-all.yaml"))
    ap.add_argument("--probe", action="store_true",
                    help="actively test nodes (TCP + GeoIP + mihomo delay/speed)")
    ap.add_argument("--probe-limit", type=int, default=1200,
                    help="probe at most N nodes, best published signal first")
    ap.add_argument("--probe-binary", default=os.environ.get("MIHOMO_BIN"),
                    help="path to mihomo; omit for TCP+GeoIP only")
    ap.add_argument("--speed-top", type=int, default=0,
                    help="measure throughput for the N lowest-delay nodes")
    ap.add_argument("--tcp-timeout", type=float, default=2.5)
    ap.add_argument("--probe-ttl", type=int, default=180,
                    help="reuse a successful probe for this many minutes")
    ap.add_argument("--max-miss", type=int, default=2,
                    help="drop a node after this many failed probe cycles")
    a = ap.parse_args()
    feeds = load("feeds.json", {})
    if a.cache:
        cache(feeds)
    rows = load_cached(feeds)
    print(f"loaded {len(rows)} nodes from {len(feeds)} feeds")
    kept, stat = organise(rows, a.health, a.speed)
    print("funnel:", dict(stat), "-> kept", len(kept))

    if a.probe and kept:
        import probe as prober
        # probe the most promising nodes first: published speed, then health
        kept.sort(key=lambda r: (-(r["md"]["speed"] or 0), -(r["md"]["health"] or 0)))
        batch = kept[:a.probe_limit]
        untested = kept[a.probe_limit:]
        probe_cache = prober.load_cache()
        fresh, reused, cooling, expired = prober.apply_cache(
            batch, probe_cache, ttl_min=a.probe_ttl, max_miss=a.max_miss)
        if fresh:
            prober.probe(fresh, binary=a.probe_binary, tcp_timeout=a.tcp_timeout,
                         speed_top=a.speed_top)
        probe_cache = prober.record(probe_cache, fresh)
        prober.save_cache(probe_cache)
        survivors = [r for r in fresh if r.get("alive")] + reused
        dropped = len(fresh) - len([r for r in fresh if r.get("alive")])
        stat["probe_dropped"] = dropped
        stat["probe_untested"] = len(untested)
        stat["probe_reused"] = len(reused)
        stat["probe_expired"] = len(expired)
        stat["probe_cooling"] = len(cooling)
        for r in survivors:
            if r.get("speed_mbps"):
                r["md"]["speed"] = r["speed_mbps"]
        kept = rank_and_rename(survivors + untested)
        print("probe:", prober.report(survivors),
              f"probed {len(fresh)}, dropped {dropped}, reused {len(reused)}, "
              f"cooling {len(cooling)}, expired {len(expired)}, untested {len(untested)}")
        save("probe.json", {**prober.report(survivors), "probed": len(fresh),
                            "reused": len(reused), "cooling": len(cooling),
                            "expired": len(expired),
                            "untested": len(untested)})

    n = emit(kept, a.out, min_region=a.min_region, max_regions=a.max_regions)
    emit(kept, a.flat, with_groups=False)
    regs = collections.Counter(r["md"]["region"] or "??" for r in kept)
    prots = collections.Counter(r["p"].get("type") for r in kept)
    speeds = sorted(r["md"]["speed"] for r in kept if r["md"]["speed"])
    print(f"emitted {n} proxies -> {a.out} (+ {a.flat})")
    print("regions:", regs.most_common(12))
    print("protocols:", prots.most_common())
    if speeds:
        print(f"speed MB/s: p10={speeds[len(speeds)//10]:.1f} med={statistics.median(speeds):.1f} p90={speeds[9*len(speeds)//10]:.1f}")
    save("organise_report.json", {"funnel": dict(stat), "kept": len(kept),
                                  "regions": dict(regs), "protocols": dict(prots)})


if __name__ == "__main__":
    main()
