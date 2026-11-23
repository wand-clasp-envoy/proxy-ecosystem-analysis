#!/usr/bin/env python
"""Measure churn: what actually changes between two published revisions.

For each feed, fetch the newest two revisions of its node file and compare:
  - node set survival  (same transport fingerprint present in both)
  - name drift         (same fingerprint, different display name)
  - region/protocol re-layout

  GH_TOKEN=$(gh auth token) python tools/churn.py
"""
import collections, json, os, re, sys
import yaml
from harvest import DATA, get, load, save
from fetch_nodes import fingerprint, geo, name_fields

PICK = ["6b745663fd038e3c63e0880ecc652bcf",  # Tdison  (96 nodes, Configuration trio)
        "b68d405379a0ffd9b686c6c497b484c1",  # HAIDEJIANG (history.yaml + sub placeholder)
        ]


def node_file(j):
    best = (None, [])
    for name, meta in j["files"].items():
        if not re.search(r"\.ya?ml$", name, re.I):
            continue
        try:
            d = yaml.safe_load(meta.get("content") or "")
        except Exception:
            continue
        px = (d or {}).get("proxies") if isinstance(d, dict) else None
        px = [p for p in (px or []) if isinstance(p, dict)]
        if len(px) > len(best[1]):
            best = (name, px)
    return best


def collect(feed_ids):
    rows = []
    for gid, meta in feed_ids.items():
        try:
            commits = json.loads(get(f"https://api.github.com/gists/{gid}/commits?per_page=100"))
        except Exception as e:
            print(gid[:8], "commits ERR", e)
            continue
        revs = []
        picks = [commits[0], commits[len(commits) // 2], commits[-1]] if len(commits) > 2 else commits
        for c in picks:
            try:
                j = json.loads(get(f"https://api.github.com/gists/{gid}/{c['version']}"))
            except Exception as e:
                break
            fname, px = node_file(j)
            if px:
                revs.append({"sha": c["version"], "at": c["committed_at"], "file": fname,
                             "fp": [fingerprint(p) for p in px],
                             "names": {fingerprint(p): str(p.get("name")) for p in px},
                             "regions": collections.Counter(geo(p.get("name")) for p in px),
                             "types": collections.Counter(p.get("type") for p in px)})
        if len(revs) < 2:
            print(f"  {meta['owner'][:16]:16s} only {len(revs)} usable revs")
            continue
        new, old = revs[0], revs[-1]
        s_new, s_old = set(new["fp"]), set(old["fp"])
        same_name = sum(1 for k in s_new & s_old if new["names"].get(k) == old["names"].get(k))
        row = {
            "owner": meta["owner"], "id": gid, "file": new["file"], "revs": len(commits),
            "new_at": new["at"], "old_at": old["at"],
            "n_new": len(s_new), "n_old": len(s_old),
            "kept": len(s_new & s_old), "added": len(s_new - s_old), "dropped": len(s_old - s_new),
            "name_kept": same_name,
            "span_h": round((__import__('datetime').datetime.fromisoformat(new['at'].replace('Z','+00:00'))
                             - __import__('datetime').datetime.fromisoformat(old['at'].replace('Z','+00:00'))
                             ).total_seconds() / 3600, 1),
            "survival": round(len(s_new & s_old) / max(1, len(s_old)), 3),
            "type_shift": dict(collections.Counter(new["types"]) - collections.Counter(old["types"])),
            "region_shift": dict(collections.Counter(new["regions"]) - collections.Counter(old["regions"])),
        }
        rows.append(row)
        print(f"  {row['owner'][:16]:16s} {row['file'][:16]:16s} old={row['n_old']:4d} new={row['n_new']:4d} "
              f"kept={row['kept']:4d} +{row['added']:3d} -{row['dropped']:3d} "
              f"survive={row['survival']:.0%} name_unchanged={row['name_kept']:3d}")
    save("churn.json", rows)


if __name__ == "__main__":
    feeds = load("feeds.json", {})
    collect(feeds)
