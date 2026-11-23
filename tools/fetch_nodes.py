#!/usr/bin/env python
"""Fetch every node-feed gist found by harvest.py, fingerprint and compare nodes.

Identity rule ("transport fingerprint"): two entries are the same node iff every
field except `name` matches. Names carry volatile telemetry (speed, health,
unlock tags, expiry), so they must never be part of identity.

  GH_TOKEN=$(gh auth token) python tools/fetch_nodes.py
"""
import base64, collections, hashlib, json, os, re, urllib.error, sys
import yaml
from harvest import DATA, get, load, save

COSMETIC = {"name", "udp", "client-fingerprint", "skip-cert-verify",
            "fingerprint", "tfo", "mptcp", "xudp", "packet-encoding",
            "global-padding", "authenticated-length", "udp-over-tcp"}
AUTH = ("password", "uuid", "username", "auth-str", "psk")


def fingerprint(p):
    body = {k: v for k, v in p.items() if k not in COSMETIC}
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def geo(name):
    name = str(name or "")
    m = re.match(r"^\s*([\U0001F1E6-\U0001F1FF]{2})\s*([A-Z]{2})\b", name)
    if m:
        return m.group(2)
    if name.startswith("❓"):
        return "??"
    m = re.search(r"\b([A-Z]{2})\b", name)
    return m.group(1) if m else "??"


def name_fields(name):
    """|speed|health?|line-landing|YT-?|GPT⁺-?|GM-?|X ..."""
    name = str(name or "")
    parts = [s.strip() for s in name.split("|")]
    out = {"label": parts[0], "gi": geo(parts[0])}
    for s in parts[1:]:
        if re.fullmatch(r"\d+(\.\d+)?\s*MB/s", s):
            out["speed"] = s
        elif re.fullmatch(r"\d{1,3}%", s):
            out["health"] = s
        elif re.fullmatch(r"\d{6}", s):
            out["expiry"] = s
        else:
            for tag in re.findall(r"(YT|GPT⁺|GM|NF|X|TK|AI|D\+)\b", s):
                out.setdefault("unlock", [])
                if tag not in out["unlock"]:
                    out["unlock"].append(tag)
            if "-" in s and not re.match(r"(YT|GPT⁺|GM)-", s):
                out.setdefault("line", []).append(s)
    return out


def load_proxies(gist):
    """Return (file_used, [proxy...]) for the biggest node-bearing file."""
    best = None
    for fname, meta in (gist.get("files") or {}).items():
        content = meta.get("content")
        if not content or not re.search(r"\.ya?ml$", fname, re.I):
            continue
        try:
            d = yaml.safe_load(content)
        except Exception:
            continue
        px = (d or {}).get("proxies") if isinstance(d, dict) else None
        if px and (best is None or len(px) > len(best[1])):
            best = (fname, px)
    return best or (None, [])


def main():
    index = load("gists.json", [])
    feeds = [g for g in index if g["signature"] and
             any(re.search(r"(all|mihomo|clash)\.ya?ml$", f, re.I) for f in g["files"])]
    print(f"{len(feeds)} candidate feeds")
    out = load("feeds.json", {})
    print(f"  resuming with {len(out)} already fetched")
    for i, g in enumerate(feeds, 1):
        gid = g["id"]
        if gid in out:
            continue
        try:
            j = json.loads(get(f"https://api.github.com/gists/{gid}"))
            commits = json.loads(get(f"https://api.github.com/gists/{gid}/commits"))
        except Exception as e:
            print(f"  {gid[:8]} ERR {e}")
            continue
        fname, px = load_proxies(j)
        px = [p for p in px if isinstance(p, dict)]
        if not px:
            print(f"  {gid[:8]} no proxies")
            continue
        fps = [fingerprint(p) for p in px]
        out[gid] = {
            "owner": g["owner"], "file": fname, "n": len(px),
            "uniq_fp": len(set(fps)), "proxies_fp": fps,
            "revs": len(commits), "first_rev": commits[-1]["committed_at"] if commits else None,
            "last_rev": commits[0]["committed_at"] if commits else None,
            "types": dict(collections.Counter(p.get("type") for p in px)),
            "names": [str(p.get("name", "")) for p in px][:400],
            "regions": dict(collections.Counter(geo(p.get("name", "")) for p in px)),
            "fields": [name_fields(p.get("name", "")) for p in px[:200]],
            "files": {k: v.get("size") for k, v in j["files"].items()},
            "size": os.path.getsize(os.path.join(DATA, "gists.json")),
        }
        if i % 5 == 0:
            save("feeds.json", out)
        print(f"  [{i}/{len(feeds)}] {g['owner'][:18]:18s} {fname:14s} n={len(px):4d} uniq={len(set(fps)):4d} revs={len(commits)}")
    save("feeds.json", out)


if __name__ == "__main__":
    main()
