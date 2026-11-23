#!/usr/bin/env python
"""Format-level comparison for feeds that publish the full trio.

Records for each feed: node count in the yaml, URI count in base64.txt,
per-protocol loss (protocols that cannot be expressed as a URI), the
proxy-group / rule template size, and which CDN the rule-providers use.

  GH_TOKEN=$(gh auth token) python tools/formats.py
"""
import base64, collections, json, re
import yaml
from harvest import DATA, get, load, save


def main():
    feeds = load("feeds.json", {})
    rows = []
    for gid, meta in feeds.items():
        try:
            j = json.loads(get(f"https://api.github.com/gists/{gid}"))
        except Exception:
            continue
        files = j["files"]
        mihomo = next((f for f in files if re.search(r"mihomo\.ya?ml$", f, re.I)), None)
        if not mihomo:
            continue
        try:
            d = yaml.safe_load(files[mihomo].get("content") or "")
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        px = [p for p in (d.get("proxies") or []) if isinstance(p, dict)]
        uri, uritypes = 0, {}
        for f in files:
            if re.search(r"base64\.txt$", f, re.I):
                raw = (files[f].get("content") or "").strip()
                try:
                    dec = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
                except Exception:
                    dec = ""
                sch = re.findall(r"^([a-z0-9]+)://", dec, re.M)
                uri = len(sch)
                uritypes = dict(collections.Counter(sch))
        rp = d.get("rule-providers") or {}
        rows.append({
            "owner": meta["owner"], "id": gid, "nodes": len(px), "uri": uri,
            "types": dict(collections.Counter(p.get("type") for p in px)),
            "uri_types": uritypes,
            "groups": len(d.get("proxy-groups") or []),
            "rules": len(d.get("rules") or []),
            "providers": len(rp),
            "provider_hosts": sorted({re.sub(r"/.*", "", re.sub(r"^https?://", "", v.get("url", "")))
                                      for v in rp.values() if isinstance(v, dict)}),
            "keys": list(d.keys()),
            "dns": bool(d.get("dns")), "tun": bool(d.get("tun")),
            "proxy_providers": bool(d.get("proxy-providers")),
        })
        print(f"  {meta['owner'][:18]:18s} nodes={len(px):4d} uri={uri:4d} "
              f"groups={rows[-1]['groups']:2d} rules={rows[-1]['rules']:2d}")
    save("formats.json", rows)
    loss = collections.Counter()
    for r in rows:
        for t, c in r["types"].items():
            loss[t] += c - r["uri_types"].get(t, 0)
    print("b64 loss by protocol:", loss.most_common())
    print("group templates:", collections.Counter((r["groups"], r["rules"]) for r in rows).most_common())


if __name__ == "__main__":
    main()
