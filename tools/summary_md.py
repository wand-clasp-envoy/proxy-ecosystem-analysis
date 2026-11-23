#!/usr/bin/env python
"""Print a markdown run summary (consumed by $GITHUB_STEP_SUMMARY)."""
import json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def load(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main():
    s = load("summary.json")
    r = load("organise_report.json")
    print("### harvest snapshot\n")
    if s:
        print(f"- feeds: **{s['feeds']}**, nodes: **{s['nodes_total']}**, "
              f"median nodes/feed: {s['nodes_median']:.0f}")
        print(f"- update cadence median: {s['cadence_median_min']:.0f} min")
        print(f"- 24h survival: median {s['surv_median']:.0%}, "
              f"≤20%: {s['surv_low_share']:.0%} of feeds")
        print(f"- group templates (groups/rules): {s['templates']}")
        loss = {k: v for k, v in s.get("b64_loss", {}).items() if v}
        if loss:
            print(f"- base64 loses: {loss}")
    if r:
        print(f"\n- kept after gates: **{r['kept']}** (funnel `{r['funnel']}`)")
        regs = sorted(r.get("regions", {}).items(), key=lambda kv: -kv[1])[:6]
        if regs:
            print(f"- top regions: {', '.join(f'{k} {v}' for k, v in regs)}")
    if not s and not r:
        print("- no data yet (pipeline step failed before report.py)")


if __name__ == "__main__":
    main()
