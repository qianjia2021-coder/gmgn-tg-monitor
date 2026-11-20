# -*- coding: utf-8 -*-
"""云端链路每轮打点：把当前轮次结果写进仓库 loop_beat.txt"""
import os, base64, json, time, urllib.request

REPO = "qianjia2021-coder/gmgn-tg-monitor"
PATH = "loop_beat.txt"
tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
if not tok:
    raise SystemExit(0)

line = "iter={} | {} | hb_age={}s | {}\n".format(
    os.environ.get("N", "?"),
    time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
    os.environ.get("AGE", "?"),
    os.environ.get("ACTION", "?"),
)

h = {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
     "User-Agent": "dahui-loop"}
url = "https://api.github.com/repos/%s/contents/%s" % (REPO, PATH)
sha = None
try:
    sha = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=15))["sha"]
except Exception:
    pass
body = {"message": "loop beat [skip ci]", "content": base64.b64encode(line.encode()).decode()}
if sha:
    body["sha"] = sha
urllib.request.urlopen(urllib.request.Request(
    url, data=json.dumps(body).encode(), method="PUT",
    headers={**h, "Content-Type": "application/json"}), timeout=20).read()
print("beat written:", line.strip())
