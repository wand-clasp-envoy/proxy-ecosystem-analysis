#!/usr/bin/env python
"""Merge the collected JSON into docs/feeds.md + data/summary.json."""
import collections, json, os, statistics as st
from harvest import DATA, ROOT, load

DOCS = os.path.join(ROOT, "docs")
GIST = "https://gist.github.com/{owner}/{id}"


def pct(x):
    return "-" if x is None else f"{x:.0%}"


def main():
    daily = {r["id"]: r for r in load("daily.json", [])}
    churn = {r["id"]: r for r in load("churn.json", [])}
    fmt = {r["id"]: r for r in load("formats.json", [])}
    feeds = load("feeds.json", {})
    gists = {(g["owner"], g["id"]): g for g in load("gists.json", [])}

    rows = []
    for gid, f in feeds.items():
        d = daily.get(gid, {})
        c = churn.get(gid, {})
        m = fmt.get(gid, {})
        g = gists.get((f["owner"], gid), {})
        rows.append({
            "owner": f["owner"], "id": gid, "file": f["file"], "n": f["n"],
            "types": f["types"], "surv": d.get("surv24", c.get("survival")),
            "cadence": d.get("cadence_min"), "groups": m.get("groups"),
            "rules": m.get("rules"), "uri": m.get("uri"),
            "created": (g.get("created") or "")[:10],
            "updated": (g.get("updated") or "")[:10],
            "revs": d.get("revs") or f.get("revs"),
        })
    rows.sort(key=lambda r: -r["n"])

    lines = ["# 采集到的节点源（gist feed）一览\n",
             "生成：`python tools/report.py`。数值来自 2026-11-23 的一次快照。\n",
             "| 发布者 | 节点文件 | 节点数 | 24h 存活 | 更新间隔 | 分组/规则 | base64 覆盖 | 创建 | 最近更新 |",
             "|---|---|---:|---:|---:|---:|---:|---|---|"]
    for r in rows:
        b64 = f"{r['uri']}/{r['n']}" if r["uri"] is not None else "-"
        gr = f"{r['groups']}/{r['rules']}" if r["groups"] is not None else "-"
        cad = "-" if not r["cadence"] else (f"{r['cadence']:.0f}m" if r["cadence"] < 120 else f"{r['cadence']/60:.0f}h")
        lines.append(f"| [{r['owner']}]({GIST.format(owner=r['owner'], id=r['id'])}) | `{r['file']}` | {r['n']} | "
                     f"{pct(r['surv'])} | {cad} | {gr} | {b64} | {r['created']} | {r['updated']} |")
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "feeds.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    ns = [r["n"] for r in rows]
    surv = [r["surv"] for r in rows if r["surv"] is not None]
    cad = [r["cadence"] for r in rows if r["cadence"]]
    q = lambda xs, p: sorted(xs)[min(len(xs) - 1, int(len(xs) * p))]
    summary = {
        "feeds": len(rows), "nodes_total": sum(ns),
        "nodes_median": st.median(ns), "nodes_p90": q(ns, .9), "nodes_max": max(ns),
        "surv_median": st.median(surv), "surv_mean": st.mean(surv),
        "surv_low_share": sum(1 for s in surv if s <= .2) / len(surv),
        "surv_high_share": sum(1 for s in surv if s >= .8) / len(surv),
        "cadence_median_min": st.median(cad), "cadence_p10": q(cad, .1), "cadence_p90": q(cad, .9),
        "trio_feeds": len(fmt), "b64_exact_feeds": sum(1 for r in fmt.values() if r["uri"] == r["nodes"]),
        "templates": {f"{r['groups']}/{r['rules']}": sum(1 for x in fmt.values()
                                                       if (x["groups"], x["rules"]) == (r["groups"], r["rules"]))
                      for r in fmt.values()},
        "b64_loss": {},
    }
    loss = collections.Counter()
    for r in fmt.values():
        for t, c in r["types"].items():
            loss[t] += c - r["uri_types"].get(t, 0)
    summary["b64_loss"] = dict(loss.most_common())
    with open(os.path.join(DATA, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"wrote {os.path.join(DOCS,'feeds.md')}")


if __name__ == "__main__":
    main()
