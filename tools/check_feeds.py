#!/usr/bin/env python
"""Fail fast when the collection step produced nothing.

The usual cause is a missing PAT: the Actions app token (GITHUB_TOKEN) is scoped
to the repository and gets 403 on /gists/{id}, which is a user endpoint.
"""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
p = os.path.join(ROOT, "data", "feeds.json")
feeds = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
if not feeds:
    sys.exit("no feeds collected - GH_TOKEN is missing or cannot read user gists; "
             "set a PAT with the `gist` scope as GH_PAT (see docs/github-actions.md)")
print(f"{len(feeds)} feeds collected")
