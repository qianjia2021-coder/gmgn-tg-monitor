#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GMGN 精选 · 云端兜底（只推送，不模拟买入）。

独立任务，不与其它云端脚本共用任何东西：
- 独立 workflow：.github/workflows/picks-cloud.yml
- 独立 secrets：PICKS_TG_API_ID / PICKS_TG_API_HASH / PICKS_TG_STRING_SESSION
- 独立会话：本机与云端各一套 auth key（共用会 AuthKeyDuplicatedError 永久作废）
- 独立状态：cloud_picks_seen.json（云端自有）+ picks_seen.json（本机上传，只读）

流程：
1. 读 picks_seen.json（本机上传的）+ cloud_picks_seen.json 取并集当已推集合
2. POST /rrs/api/v1/gmgn_go_hot_searches 取 position 前三
3. 未推过的逐个发 TG（HTML，合约地址 code 可单击复制、保留大小写）
4. 写回 cloud_picks_seen.json 与 picks_cloud_hb.txt

不调用 dbotx：云端只负责记录与推送。
"""
import asyncio
import base64
import gzip
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY", "qianjia2021-coder/gmgn-tg-monitor")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
BRANCH = "main"

SEEN_LOCAL = "picks_seen.json"          # 本机上传，云端只读
SEEN_CLOUD = "cloud_picks_seen.json"    # 云端自有
HB_CLOUD = "picks_cloud_hb.txt"

TG_API_ID = os.environ.get("PICKS_TG_API_ID", "")
TG_API_HASH = os.environ.get("PICKS_TG_API_HASH", "")
TG_SESSION = os.environ.get("PICKS_TG_STRING_SESSION", "")
CHAT_ID = int(os.environ.get("PICKS_CHAT_ID", "-5589667040"))

TOP_N = 3
RRS_API = "https://gmgn.ai/rrs/api/v1/gmgn_go_hot_searches"
RRS_QUERY = (
    "?device_id=24cfd489-1084-444a-8645-4ed1c52a62e9&tab_id=picks_cloud"
    "&fp_did=433ccd53ca66e0ddd8b3c3379fe95017&client_id=gmgn_web_20261124-903-fcc796f"
    "&from_app=gmgn&app_ver=20261124-903-fcc796f&tz_name=Asia/Shanghai&tz_offset=28800"
    "&app_lang=zh-CN&os=web"
)
RRS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
RRS_TIMEOUT = 12
FETCH_TRIES = 3

CHAIN_LABEL = {
    "sol": "SOL", "bsc": "BSC", "eth": "ETH", "base": "BASE",
    "robinhood": "RH", "rh": "RH", "elysium": "ELYS", "elys": "ELYS",
    "arbitrum": "ARB", "hyperevm": "HYPER", "arc": "ARC", "stable": "STABLE",
}
CHAIN_SEG = {
    "sol": "sol", "bsc": "bsc", "eth": "eth", "base": "base",
    "robinhood": "robinhood", "rh": "robinhood", "elysium": "elysium", "elys": "elysium",
}


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- GitHub 读写
_sha_cache = {}


def _gh_request(method, path, payload=None):
    url = "https://api.github.com/repos/%s/contents/%s" % (REPO, path)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "token " + GH_TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "picks-cloud")
    if body:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def gh_get(path):
    """读仓库文件，返回文本；不存在返回 None。"""
    try:
        d = _gh_request("GET", path + "?ref=" + BRANCH)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    _sha_cache[path] = d.get("sha")
    return base64.b64decode(d.get("content") or "").decode("utf-8", "replace")


def gh_put(path, text, message):
    """写仓库文件；409/422 视为 sha 过期，重取后重试。"""
    for attempt in range(4):
        sha = None
        try:
            d = _gh_request("GET", path + "?ref=" + BRANCH)
            sha = d.get("sha")
        except urllib.error.HTTPError:
            sha = None
        payload = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": BRANCH,
        }
        if sha:
            payload["sha"] = sha
        try:
            r = _gh_request("PUT", path, payload)
        except urllib.error.HTTPError as e:
            if e.code in (409, 422, 500, 502, 503):
                log("写 %s 失败 HTTP %s，重试 %d/4" % (path, e.code, attempt + 1))
                time.sleep(2 + attempt * 2)
                continue
            raise
        _sha_cache[path] = (r.get("content") or {}).get("sha")
        return True
    return False


def load_pushed(path):
    txt = gh_get(path)
    if not txt:
        return set()
    try:
        return set(str(x) for x in (json.loads(txt).get("pushed") or []))
    except Exception as e:
        log("解析 %s 失败，按空集处理：%s %s" % (path, type(e).__name__, str(e)[:80]))
        return set()


# ---------------------------------------------------------------- 榜单
def _fmt_money(v):
    try:
        x = float(v)
    except Exception:
        return ""
    if x <= 0:
        return ""
    if x >= 1e9:
        return "$%.2fB" % (x / 1e9)
    if x >= 1e6:
        return "$%.2fM" % (x / 1e6)
    if x >= 1e3:
        return "$%.1fK" % (x / 1e3)
    return "$%.0f" % x


def _age_text(ts, now):
    try:
        v = float(ts)
    except Exception:
        return ""
    if v <= 0:
        return ""
    if v > 1e12:
        v /= 1000.0
    d = int(now - v)
    if d < 0:
        return ""
    if d < 60:
        return "%ds" % d
    if d < 3600:
        return "%dm" % (d // 60)
    if d < 86400:
        return "%dh" % (d // 3600)
    return "%dd" % (d // 86400)


def fetch_rows():
    data = b"{}"
    for i in range(FETCH_TRIES):
        req = urllib.request.Request(RRS_API + RRS_QUERY, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Accept-Encoding", "gzip")
        req.add_header("User-Agent", RRS_UA)
        req.add_header("Origin", "https://gmgn.ai")
        req.add_header("Referer", "https://gmgn.ai/trend?tab=picks&chain=sol")
        try:
            with urllib.request.urlopen(req, timeout=RRS_TIMEOUT) as r:
                raw = r.read()
                if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                payload = json.loads(raw.decode("utf-8", "replace"))
            if not isinstance(payload, dict) or payload.get("code") != 0:
                log("接口返回异常：%s" % str(payload)[:150])
                continue
            toks = ((payload.get("data") or {}).get("tokens")) or []
            now = time.time()
            rows = []
            for t in toks:
                if not isinstance(t, dict):
                    continue
                chain = (t.get("chain") or "").lower().strip()
                addr = (t.get("address") or "").strip()
                if not chain or len(addr) < 20:
                    continue
                rows.append({
                    "chain": chain,
                    "address": addr,
                    "symbol": (t.get("symbol") or "").strip()[:40],
                    "age": _age_text(t.get("open_timestamp") or t.get("creation_timestamp"), now),
                    "mc": _fmt_money(t.get("market_cap")),
                    "vol24": _fmt_money(t.get("volume")),
                    "pos": int(t.get("position") or 999999),
                })
            rows.sort(key=lambda x: x["pos"])
            if rows:
                return rows
        except Exception as e:
            log("第 %d 次抓取失败：%s %s" % (i + 1, type(e).__name__, str(e)[:100]))
            time.sleep(1.5 + i)
    return None


# ---------------------------------------------------------------- 消息
def token_url(chain, address):
    return "https://gmgn.ai/%s/token/%s" % (CHAIN_SEG.get(chain, chain), address)


def render(row, rank):
    lines = ["⭐ <b>GMGN 精选 · 第 %d 名</b>" % rank, ""]
    lines.append("<b>%s</b>" % html.escape(row["symbol"] or "未知代币"))
    lines.append("链：%s" % CHAIN_LABEL.get(row["chain"], row["chain"].upper()))
    lines.append("合约：<code>%s</code>" % html.escape(row["address"]))
    detail = []
    if row.get("age"):
        detail.append("上线 %s" % row["age"])
    if row.get("mc"):
        detail.append("市值 %s" % row["mc"])
    if row.get("vol24"):
        detail.append("24h 成交额 %s" % row["vol24"])
    if detail:
        lines.append(" | ".join(detail))
    lines.append("")
    lines.append("打开：%s" % token_url(row["chain"], row["address"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程
async def run():
    if not GH_TOKEN:
        log("缺少 GITHUB_TOKEN，无法读写状态")
        return 2
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        log("缺少 PICKS_TG_* 会话配置，本轮只记录不推送")
        return 0

    local_seen = load_pushed(SEEN_LOCAL)
    cloud_seen = load_pushed(SEEN_CLOUD)
    done = local_seen | cloud_seen
    log("已推集合：本机 %d + 云端 %d = %d" % (len(local_seen), len(cloud_seen), len(done)))

    rows = fetch_rows()
    if rows is None:
        log("连续 %d 次抓取失败，本轮放弃" % FETCH_TRIES)
        return 1
    log("榜单 %d 条，前三：%s" % (
        len(rows), " / ".join("%s(%s)" % (r["symbol"], r["chain"]) for r in rows[:TOP_N])))

    top = rows[:TOP_N]
    new_rows = [r for r in top if "%s:%s" % (r["chain"], r["address"]) not in done]
    if not new_rows:
        log("前三名都推过，本轮无推送")
        gh_put(HB_CLOUD, json.dumps({"ts": int(time.time()), "rows": len(rows), "new": 0}), "picks cloud hb")
        return 0

    from telethon import TelegramClient
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(TG_SESSION), int(TG_API_ID), TG_API_HASH, timeout=30)
    await client.connect()
    pushed_now = []
    try:
        if not await client.is_user_authorized():
            log("云端会话未授权（可能已失效），本轮只记录不推送")
            return 3
        for r in new_rows:
            key = "%s:%s" % (r["chain"], r["address"])
            rank = top.index(r) + 1
            text = render(r, rank)
            ok = False
            for i in range(3):
                try:
                    await client.send_message(CHAT_ID, text, parse_mode="html", link_preview=False)
                    ok = True
                    break
                except Exception as e:
                    log("发送失败(%d/3) %s：%s %s" % (i + 1, key, type(e).__name__, str(e)[:90]))
                    await asyncio.sleep(2 + i * 2)
            if ok:
                pushed_now.append(key)
                log("已推送 %s %s" % (key, r["symbol"]))
            await asyncio.sleep(1)
    finally:
        await client.disconnect()

    if pushed_now:
        cloud_seen |= set(pushed_now)
        gh_put(SEEN_CLOUD, json.dumps({"pushed": sorted(cloud_seen), "updated": int(time.time())},
                                      ensure_ascii=False), "picks cloud seen +%d" % len(pushed_now))
    gh_put(HB_CLOUD, json.dumps({"ts": int(time.time()), "rows": len(rows), "new": len(pushed_now)},
                                ensure_ascii=False), "picks cloud hb")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
