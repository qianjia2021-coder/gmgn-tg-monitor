#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GMGN 热搜第一监控 -> Telegram「GMGN热搜」群组（云端版，GitHub Actions）
=========================================================================
与本地 hot_search_monitor.py 同一逻辑：SOL + BSC 1h 热搜榜，rank==1 换新合约即推送。
双端共享状态文件 cloud_hot_state.json（存于仓库），本地/云端双向同步去重；
推送前再扫群内最近 30 条消息兜底，防止本地/云端竞态重复。

Env:
  TG_API_ID / TG_API_HASH / TG_STRING_SESSION   Telegram 用户会话
  GMGN_API_KEY                                    gmgn-cli 配置
  GITHUB_TOKEN                                    Actions 自动提供，用于读写状态文件
  TG_HOTSEARCH_CHAT_ID                           目标群（默认 5499948080 = GMGN热搜）
"""
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import InputPeerChat

REPO = "qianjia2021-coder/gmgn-tg-monitor"
STATE_PATH = "cloud_hot_state.json"
GH_API = "https://api.github.com"
CHAINS = ["sol", "bsc"]
HOT_INTERVAL = "1h"
LIMIT = 20
TARGET_CHAT = int(os.environ.get("TG_HOTSEARCH_CHAT_ID", "5499948080"))
TARGET_NAME = "GMGN热搜"
ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b|\b0x[a-fA-F0-9]{40}\b")


def gh_headers():
    return {
        "Authorization": "token " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
        "User-Agent": "gmgn-hotsearch-monitor",
    }


def read_state():
    r = requests.get(f"{GH_API}/repos/{REPO}/contents/{STATE_PATH}",
                     headers=gh_headers(), timeout=20)
    if r.status_code == 404:
        return {}, None
    r.raise_for_status()
    j = r.json()
    content = base64.b64decode(j["content"]).decode("utf-8")
    return json.loads(content), j["sha"]


def write_state(state, sha):
    data = json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8")
    body = {
        "message": "sync hot state from cloud [skip ci]",
        "content": base64.b64encode(data).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{REPO}/contents/{STATE_PATH}",
                     headers=gh_headers(), json=body, timeout=30)
    r.raise_for_status()


def run_gmgn(args):
    for attempt in (1, 2):
        proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
        if proc.returncode == 0:
            return proc.stdout
        err = (proc.stderr or proc.stdout or "")
        if "429" in err and attempt == 1:
            print("429 rate limited, waiting 35s and retrying...")
            time.sleep(35)
            continue
        raise RuntimeError("gmgn-cli failed rc={}: {}".format(proc.returncode, err[-500:]))


def fetch_hot(chain):
    args = ["gmgn-cli", "market", "hot-searches", "--chain", chain,
            "--interval", HOT_INTERVAL, "--limit", str(LIMIT), "--raw"]
    print("cmd: " + " ".join(args))
    out = run_gmgn(args)
    data = json.loads(out)
    if isinstance(data, list) and data:
        return data[0].get("tokens") or []
    return (data.get("data") or {}).get("rank") or []


def get_rank1(tokens):
    for t in tokens:
        if int(t.get("rank") or 0) == 1:
            return t
    return tokens[0] if tokens else None


def fmt_msg(t, chain):
    sym = t.get("symbol") or ""
    name = t.get("name") or ""
    addr = t.get("address") or ""
    heat = t.get("visiting_count") or 0
    chg1h = round(float(t.get("price_change_percent1h") or 0), 1)
    mc = round(float(t.get("market_cap") or 0))
    liq = round(float(t.get("liquidity") or 0))
    lp = t.get("launchpad_platform") or ""
    holders = int(t.get("holder_count") or 0)
    swaps = int(t.get("swaps") or 0)
    return "\n".join([
        "🔥 <b>GMGN 热搜第一</b>  #{} {}".format(sym, name),
        "链: {} | 平台: {} | 热搜热度: {}".format(chain.upper(), lp or "-", heat),
        "1h涨跌: {}% | 市值 ${:,} | 流动性 ${:,}".format(chg1h, mc, liq),
        "持有人: {:,} | swaps: {:,}".format(holders, swaps),
        "GMGN: https://gmgn.ai/{}/token/{}".format(chain, addr),
        "<code>{}</code>".format(addr),
    ])


async def main():
    state, sha = read_state()
    print("state entries: {} chains".format(len(state)))
    client = TelegramClient(StringSession(os.environ["TG_STRING_SESSION"]),
                            int(os.environ["TG_API_ID"]),
                            os.environ["TG_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        print("FATAL: not authorized")
        sys.exit(1)
    peer = InputPeerChat(TARGET_CHAT)
    # 兜底去重：群内最近 30 条消息里已出现的合约
    already = set()
    async for msg in client.iter_messages(peer, limit=30):
        if msg.message:
            for m in ADDR_RE.findall(msg.message):
                already.add(m.lower())
    print("already in group (last 30): {}".format(len(already)))
    pushed = 0
    changed = False
    for chain in CHAINS:
        try:
            tokens = fetch_hot(chain)
        except Exception as e:
            print("[{}] fetch failed: {}".format(chain, e))
            continue
        t1 = get_rank1(tokens)
        if not t1:
            print("[{}] empty rank".format(chain))
            continue
        addr = (t1.get("address") or "").strip()
        sym = t1.get("symbol") or ""
        print("[{}] hot #1: {} | {}".format(chain, sym, addr))
        rec = state.get(chain) or {"last": "", "seen": []}
        seen = set(rec.get("seen") or [])
        if addr in seen or addr.lower() in already:
            print("[{}] already pushed, skip".format(chain))
            rec["last"] = addr
            state[chain] = rec
            continue
        try:
            await client.send_message(peer, fmt_msg(t1, chain),
                                      parse_mode="html", link_preview=False)
            print("[{}] PUSHED hot #1: {} | {}".format(chain, sym, addr))
        except Exception as e:
            print("[{}] push failed: {}".format(chain, e))
            continue
        seen.add(addr)
        rec["last"] = addr
        rec["seen"] = sorted(seen)
        state[chain] = rec
        pushed += 1
        changed = True
        await asyncio.sleep(1.5)
    await client.disconnect()
    if changed:
        try:
            write_state(state, sha)
            print("state written")
        except Exception as e:
            print("state write failed: {}".format(e))
    print("done pushed={}".format(pushed))


if __name__ == "__main__":
    asyncio.run(main())
