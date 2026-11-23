#!/usr/bin/env python
"""Publish the organised subscription to a GitHub gist.

The gist is the "delivery" layer of the ecosystem: consumers point their client
at https://gist.githubusercontent.com/<user>/<gist>/raw/<file>.

  GH_TOKEN=<pat with gist scope> python tools/publish.py --gist <id> --file data/organised.yaml
  GH_TOKEN=... python tools/publish.py --dry-run        # inspect payload only

Token needs the `gist` scope (classic) or Gists:read+write (fine-grained).
`GITHUB_TOKEN` in Actions cannot create gists - use a PAT secret.
"""
import argparse, json, os, sys, urllib.error, urllib.request

API = "https://api.github.com"


def call(method, url, token, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "proxy-ecosystem-analysis"})
    return json.loads(urllib.request.urlopen(req, timeout=90).read())


def build_payload(files, desc, public):
    out = {}
    for path in files:
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            out[name] = {"content": f.read()}
    return {"description": desc, "public": public, "files": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", action="append", default=None,
                    help="file to publish (repeatable); default data/organised.yaml")
    ap.add_argument("--gist", default=os.environ.get("GIST_ID", ""),
                    help="existing gist id; omit to create a new one")
    ap.add_argument("--desc", default="clash/mihomo subscription (auto-generated)")
    ap.add_argument("--public", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-proxies", type=int, default=100,
                    help="refuse to publish a yaml with fewer proxies (safety net)")
    a = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = a.file or [os.path.join(root, "data", "organised.yaml")]
    files = [f if os.path.isabs(f) else os.path.join(root, f) for f in files]
    missing = [f for f in files if not os.path.exists(f)]
    if missing:
        sys.exit(f"missing input: {missing} (run tools/organize.py first)")

    payload = build_payload(files, a.desc, a.public)
    for name, item in payload["files"].items():
        if not name.endswith((".yaml", ".yml")):
            continue
        try:
            import yaml as _yaml
            n = len((_yaml.safe_load(item["content"]) or {}).get("proxies") or [])
        except Exception as e:
            sys.exit(f"refusing to publish unparsable {name}: {e}")
        if n < a.min_proxies:
            sys.exit(f"refusing to publish {name}: {n} proxies < --min-proxies {a.min_proxies}")
    total = sum(len(v["content"]) for v in payload["files"].values())
    print(f"files: {list(payload['files'])}  {total/1024:.0f} KB  public={a.public}")

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if a.dry_run:
        print(json.dumps({"description": a.desc, "public": a.public,
                          "files": {k: f"<{len(v['content'])} chars>" for k, v in payload["files"].items()}},
                         ensure_ascii=False, indent=1))
        return
    if not token:
        sys.exit("no token: set GH_TOKEN (needs `gist` scope)")

    try:
        if a.gist:
            res = call("PATCH", f"{API}/gists/{a.gist}", token, payload)
            print(f"updated gist {a.gist}")
        else:
            res = call("POST", f"{API}/gists", token, payload)
            print(f"created gist {res['id']}  -> put it in secret GIST_ID")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        hint = {401: "token invalid/expired",
                403: "token missing `gist` scope, or rate limited",
                404: "gist not found (wrong id, or token lacks access)"}.get(e.code, "")
        sys.exit(f"HTTP {e.code}: {hint}\n{body}")

    print(f"html: {res['html_url']}")
    for name in payload["files"]:
        raw = res.get("files", {}).get(name, {}).get("raw_url")
        if raw:
            print(f"raw:  {raw}")


if __name__ == "__main__":
    main()
