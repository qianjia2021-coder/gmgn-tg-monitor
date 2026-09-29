#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
监控 @FindTheGoldenDoge 群里 @justelviin 的发言 -> 转发到「转发群」（云端版）
================================================================================
GitHub Actions 每 5 分钟跑一次，补本地关机期间漏掉的 @justelviin 发言。
双端共享 cloud_elviin_state.json（已转发的源群消息 id 并集），转发前再扫目标群
最近 20 条 forward 消息兜底，防止本地/云端竞态重复转发。

Env:
  TG_API_ID / TG_API_HASH / TG_STRING_SESSION   Telegram 用户会话
  GITHUB_TOKEN                                   Actions 自动提供
  TG_SOURCE_GROUP                                源群 username（默认 FindTheGoldenDoge）
  TG_TARGET_USER_ID                              监控对象（默认 6537207453 = justelviin）
  TG_TARGET_CHAT_ID                              目标群（默认 5327991953 = 转发群）
"""
import asyncio
import base64
import json
import os
import sys

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

REPO = "qianjia2021-coder/gmgn-tg-monitor"
STATE_PATH = "cloud_elviin_state.json"
GH_API = "https://api.github.com"

SOURCE_GROUP = os.environ.get("TG_SOURCE_GROUP", "FindTheGoldenDoge")
TARGET_USER_ID = int(os.environ.get("TG_TARGET_USER_ID", "6537207453"))
TARGET_CHAT_ID = int(os.environ.get("TG_TARGET_CHAT_ID", "5327991953"))
BACKFILL_LIMIT = 50          # 每次拉源群最近 50 条
TARGET_RECENT_LIMIT = 20     # 扫目标群最近 20 条做兜底去重


def gh_headers():
    return {
        "Authorization": "token " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
        "User-Agent": "elviin-forward-cloud",
    }


def read_state():
    r = requests.get(f"{GH_API}/repos/{REPO}/contents/{STATE_PATH}",
                     headers=gh_headers(), timeout=20)
    if r.status_code == 404:
        return set(), None
    r.raise_for_status()
    j = r.json()
    content = base64.b64decode(j["content"]).decode("utf-8")
    data = json.loads(content)
    seen = set(str(x) for x in (data.get("seen") or [])) if isinstance(data, dict) else set()
    return seen, j["sha"]


def write_state(seen, sha, msg):
    payload = json.dumps({"seen": sorted(seen)}, ensure_ascii=False, indent=1).encode("utf-8")
    body = {
        "message": msg,
        "content": base64.b64encode(payload).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{REPO}/contents/{STATE_PATH}",
                     headers=gh_headers(), json=body, timeout=30)
    r.raise_for_status()


async def main():
    seen, sha = read_state()
    print("state seen: {} msgs".format(len(seen)))

    client = TelegramClient(StringSession(os.environ["TG_STRING_SESSION"]),
                            int(os.environ["TG_API_ID"]),
                            os.environ["TG_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        print("FATAL: not authorized")
        sys.exit(1)

    src = await client.get_entity(SOURCE_GROUP)
    tgt = await client.get_entity(TARGET_CHAT_ID)

    # 兜底去重：扫目标群最近 forward 消息，已转发过的源群消息 id
    already = set(seen)
    async for msg in client.iter_messages(tgt, limit=TARGET_RECENT_LIMIT):
        f = getattr(msg, "fwd_from", None)
        if not f:
            continue
        if getattr(f, "from_id", None) is not None and getattr(f.from_id, "user_id", None) == TARGET_USER_ID:
            # 超级群转发后 channel_post 即原消息 id
            opid = getattr(f, "channel_post", None)
            if opid:
                already.add(str(opid))
    print("already (state + target recent): {}".format(len(already)))

    pushed = 0
    async for msg in client.iter_messages(src, limit=BACKFILL_LIMIT):
        if msg.sender_id != TARGET_USER_ID:
            continue
        mid = str(msg.id)
        if mid in already:
            continue
        try:
            await client.forward_messages(tgt, msg)
            print("forwarded msg_id={} | {}".format(mid, (msg.message or "[media]")[:60].replace("\n", " ")))
            already.add(mid)
            seen.add(mid)
            pushed += 1
        except Exception as e:
            print("forward failed msg_id={}: {}".format(mid, e))
        await asyncio.sleep(1)

    await client.disconnect()

    if pushed:
        try:
            write_state(seen, sha, "sync elviin forwarded state from cloud [skip ci]")
            print("state written, total seen={}".format(len(seen)))
        except Exception as e:
            print("state write failed: {}".format(e))
    else:
        print("nothing new to forward")
    print("done pushed={}".format(pushed))


if __name__ == "__main__":
    asyncio.run(main())
