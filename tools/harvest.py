#!/usr/bin/env python
"""Harvest public gist 'subscription' sources.

Two stages:
  search  - scrape gist search results (top-N newest per query, paginated via p=)
  meta    - fetch gist metadata through the authenticated GitHub API

Why the split: gist.github.com is unreachable without the local proxy here,
while api.github.com works direct and is fast. Keep them separate.

  python tools/harvest.py search --pages 5
  python tools/harvest.py meta
"""
import json, os, re, sys, time, urllib.parse, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
PROXY = "http://127.0.0.1:7897"
QUERIES = ["clash", "mihomo", "sing-box", "v2ray", "订阅", "subs-check",
           "hysteria2", "all.yaml", "base64.txt", "机场"]

# name -> we only keep gists whose file set looks like a node feed
SIG = re.compile(r"(all\.ya?ml|mihomo\.ya?ml|base64\.txt|proxies\.ya?ml|sub|history\.ya?ml|clash.*\.ya?ml)", re.I)


def opener(use_proxy: bool):
    handlers = []
    if use_proxy:
        handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def get(url, use_proxy=False, timeout=45, headers=None):
    h = {"User-Agent": "Mozilla/5.0 (proxy-ecosystem-analysis)",
         "Accept": "application/vnd.github+json"}
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok and "api.github.com" in url:
        h["Authorization"] = "Bearer " + tok.strip()
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    return opener(use_proxy).open(req, timeout=timeout).read()


def load(name, default):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save(name, obj):
    os.makedirs(DATA, exist_ok=True)
    p = os.path.join(DATA, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    print(f"  wrote {p} ({os.path.getsize(p)} B)")


def parse_search(html):
    """Return [(owner, id, description, updated, files_hint)] from a results page."""
    out = []
    for block in re.split(r'<div class="gist-snippet">', html)[1:]:
        m = re.search(r'href="/([A-Za-z0-9_.-]+)/([0-9a-f]{20,40})"', block)
        if not m:
            continue
        owner, gid = m.group(1), m.group(2)
        desc = re.search(r'<span class="f6 color-fg-muted">(.*?)</span>', block, re.S)
        desc = re.sub(r"<[^>]+>", " ", desc.group(1)).strip() if desc else ""
        upd = re.search(r'datetime="([^"]+)"', block)
        files = re.findall(r'title="([^"]+)"', block)
        out.append({"owner": owner, "id": gid, "desc": desc[:120],
                    "updated": upd.group(1) if upd else "", "files_hint": files[:12]})
    return out


def cmd_search(pages):
    cand = {(c["owner"], c["id"]): c for c in load("candidates.json", [])}
    for q in QUERIES:
        for p in range(1, pages + 1):
            url = ("https://gist.github.com/search?o=desc&s=updated&q="
                   + urllib.parse.quote(q) + f"&p={p}")
            try:
                html = get(url, use_proxy=True).decode("utf-8", "ignore")
            except Exception as e:
                print(f"  {q} p{p}: ERR {e}")
                continue
            rows = parse_search(html)
            new = 0
            for r in rows:
                r["query"] = q
                if (r["owner"], r["id"]) not in cand:
                    cand[(r["owner"], r["id"])] = r
                    new += 1
            print(f"  {q} p{p}: {len(rows)} rows, +{new} new (total {len(cand)})")
            time.sleep(0.6)
        save("candidates.json", list(cand.values()))


def cmd_meta():
    cand = load("candidates.json", [])
    metas = {(m["owner"], m["id"]): m for m in load("gists.json", [])}
    todo = [c for c in cand if (c["owner"], c["id"]) not in metas]
    print(f"  {len(cand)} candidates, {len(todo)} to fetch")
    for i, c in enumerate(todo, 1):
        try:
            j = json.loads(get(f"https://api.github.com/gists/{c['id']}"))
        except urllib.error.HTTPError as e:
            print(f"  {c['owner']}/{c['id']}: HTTP {e.code}")
            continue
        except Exception as e:
            print(f"  {c['owner']}/{c['id']}: ERR {e}")
            continue
        files = {k: v.get("size") for k, v in j["files"].items()}
        sig = any(SIG.search(n) for n in files) or j.get("description") in ("Configuration",)
        metas[(c["owner"], c["id"])] = {
            "owner": c["owner"], "id": c["id"], "desc": j.get("description"),
            "created": j.get("created_at"), "updated": j.get("updated_at"),
            "comments": j.get("comments"), "files": files, "signature": sig,
            "queries": sorted({c.get("query")}),
        }
        if i % 50 == 0:
            save("gists.json", list(metas.values()))
        time.sleep(0.1)
    save("gists.json", list(metas.values()))
    n = sum(1 for m in metas.values() if m["signature"])
    print(f"  signature-positive: {n}/{len(metas)}")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "search"
    pages = int(sys.argv[sys.argv.index("--pages") + 1]) if "--pages" in sys.argv else 5
    {"search": lambda: cmd_search(pages), "meta": cmd_meta}[what]()
