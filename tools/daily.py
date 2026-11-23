#!/usr/bin/env python
"""Daily-window churn + format lossiness for each feed.

For every feed:
  * cadence        - median minutes between the last N revisions
  * survival/24h   - node fingerprints present in both the newest revision and
                     the newest revision that is at least 24h old
  * name churn     - of the survivors, how many kept an identical display name
  * formats        - node count in all.yaml vs URI count in base64.txt vs
                     group/rule presence in mihomo.yaml

  GH_TOKEN=$(gh auth token) python tools/daily.py
"""
import collections, datetime as dt, json, os, re, statistics, base64
import yaml
from harvest import get, load, save
from fetch_nodes import fingerprint, geo, name_fields

H = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
SCHEME = re.compile(r"^(ss|ssr|vmess|vless|trojan|hysteria2?|hy2|http|https|socks5?|anytls|tuic)://", re.M)


def proxies_from(files):
    best = (None, [])
    for name, meta in files.items():
        if not re.search(r"\.ya?ml$", name, re.I):
            continue
        try:
            d = yaml.safe_load(meta.get("content") or "")
        except Exception:
            continue
        px = [p for p in ((d or {}).get("proxies") or []) if isinstance(p, dict)] if isinstance(d, dict) else []
        if len(px) > len(best[1]):
            best = (name, px)
    return best


def b64_schemes(files):
    for name, meta in files.items():
        if not re.search(r"base64\.txt$|\.txt$", name, re.I):
            continue
        raw = (meta.get("content") or "").strip()
        if not raw:
            continue
        try:
            dec = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
        except Exception:
            continue
        sch = SCHEME.findall(dec)
        if sch:
            return len(sch), dict(collections.Counter("hy2" if s == "hysteria2" else s for s in sch))
    return 0, {}


def main():
    feeds = load("feeds.json", {})
    rows = []
    for i, (gid, meta) in enumerate(feeds.items(), 1):
        try:
            j = json.loads(get(f"https://api.github.com/gists/{gid}"))
            commits = json.loads(get(f"https://api.github.com/gists/{gid}/commits?per_page=100"))
        except Exception as e:
            print(f"  {meta['owner'][:16]:16s} ERR {e}")
            continue
        if not commits:
            continue
        fname, px = proxies_from(j["files"])
        if not px:
            continue
        times = [H(c["committed_at"]) for c in commits]
        gaps = [(a - b).total_seconds() / 60 for a, b in zip(times, times[1:])]
        cand = [c for c in commits if (times[0] - H(c["committed_at"])).total_seconds() >= 86400]
        old_c = cand[0] if cand else None
        span_h = round((times[0] - H(old_c["committed_at"])).total_seconds() / 3600, 1) if old_c else None
        surv = name_kept = added = dropped = None
        if old_c:
            try:
                oj = json.loads(get(f"https://api.github.com/gists/{gid}/{old_c['version']}"))
            except Exception:
                oj = None
            if oj:
                _, opx = proxies_from(oj["files"])
                ofp = {fingerprint(p): str(p.get("name")) for p in opx}
                nfp = {fingerprint(p): str(p.get("name")) for p in px}
                surv = len(set(ofp) & set(nfp)) / max(1, len(ofp))
                name_kept = sum(1 for k in set(ofp) & set(nfp) if ofp[k] == nfp[k])
                added, dropped = len(set(nfp) - set(ofp)), len(set(ofp) - set(nfp))
        n_uri, schemes = b64_schemes(j["files"])
        rows.append({
            "owner": meta["owner"], "id": gid, "file": fname, "n": len(px),
            "types": dict(collections.Counter(p.get("type") for p in px)),
            "regions": dict(collections.Counter(geo(p.get("name")) for p in px)),
            "revs": len(commits), "cadence_min": round(statistics.median(gaps), 1) if gaps else None,
            "span_h": span_h, "surv24": round(surv, 3) if surv is not None else None,
            "name_kept": name_kept, "added": added, "dropped": dropped,
            "uri_n": n_uri, "uri_types": schemes,
            "has_b64": any(re.search(r"base64\.txt$", f, re.I) for f in j["files"]),
            "has_mihomo": any(re.search(r"mihomo\.ya?ml$", f, re.I) for f in j["files"]),
            "files": sorted(j["files"]),
            "fp": [fingerprint(p) for p in px],
            "sample_names": [str(p.get("name")) for p in px[:5]],
        })
        if i % 10 == 0:
            save("daily.json", rows)
    save("daily.json", rows)


if __name__ == "__main__":
    main()
