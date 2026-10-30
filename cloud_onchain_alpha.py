#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Onchain Alpha Trench 群 -> GMGN热搜群 合约转发监控（云端版，GitHub Actions）
=========================================================================
与本地 onchain_alpha_monitor.py 同规则：扫 @onchainalphatrench 源群消息，
提取首次出现的合约地址（EVM/SOL 任意链），推送到 GMGN热搜群 (5499948080)。
状态 cloud_onchain_alpha_state.json 存仓库，双端共享去重。
心跳 heartbeat.txt 由本地守护每轮更新；workflow 已在前置步骤判断
「本地在线(<4min)则跳过，本地关机则云端接管」。

Env:
  TG_API_ID / TG_API_HASH / TG_STRING_SESSION   Telegram 用户会话
  GITHUB_TOKEN                                  Actions 自动提供
  TG_TARGET_CHAT_ID                             目标群（默认 5499948080）
"""
import asyncio
import base64
import json
import os
import re
import sys
import time

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import InputPeerChat

REPO = "qianjia2021-coder/gmgn-tg-monitor"
STATE_PATH = "cloud_onchain_alpha_state.json"
GH_API = "https://api.github.com"

SOURCE_CHANNEL = "onchainalphatrench"
TARGET_CHAT = int(os.environ.get("TG_TARGET_CHAT_ID", "5499948080"))
SCAN_LIMIT = 40
ADDR_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def gh_headers():
    return {
        "Authorization": "token " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
        "User-Agent": "onchain-alpha-monitor",
    }


def read_json_file(path):
    r = requests.get(f"{GH_API}/repos/{REPO}/contents/{path}",
                     headers=gh_headers(), timeout=20)
    if r.status_code == 404:
        return {"seen": []}, None
    r.raise_for_status()
    j = r.json()
    content = base64.b64decode(j["content"]).decode("utf-8")
    data = json.loads(content)
    if not isinstance(data, dict) or "seen" not in data:
        data = {"seen": []}
    return data, j["sha"]


def write_json_file(path, data, sha, msg):
    payload = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    body = {"message": msg, "content": base64.b64encode(payload).decode(),
            "branch": "main"}
    if sha:
        body["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{REPO}/contents/{path}",
                     headers=gh_headers(), json=body, timeout=30)
    r.raise_for_status()


def extract_addr(text):
    for m in ADDR_RE.findall(text or ""):
        return m.lower()
    return None


def title_line(text):
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    first = ADDR_RE.sub("", first)
    first = re.sub(r"\s*\|\s*", " · ", first).strip(" ·")
    return first


def fmt_push(text, addr):
    t = title_line(text)
    chain_tag = "EVM" if addr.startswith("0x") else "SOL"
    return "\n".join([
        "📡 <b>Onchain Alpha Trench 新合约</b>",
        t or "(无标题)",
        "链: {} | 合约: <code>{}</code>".format(chain_tag, addr),
    ])


async def main():
    state, sha = read_json_file(STATE_PATH)
    seen = set(state.get("seen") or [])
    print("state seen: {}".format(len(seen)))

    client = TelegramClient(StringSession(os.environ["TG_STRING_SESSION"]),
                            int(os.environ["TG_API_ID"]),
                            os.environ["TG_API_HASH"])
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("FATAL: not authorized")
            sys.exit(1)

        src = await client.get_entity(SOURCE_CHANNEL)
        tgt = InputPeerChat(TARGET_CHAT)

        # 兜底去重：目标群最近 30 条
        already = set()
        async for msg in client.iter_messages(tgt, limit=30):
            if msg.message:
                for m in ADDR_RE.findall(msg.message):
                    already.add(m.lower())
        print("already in target group (last 30): {}".format(len(already)))

        msgs = []
        async for msg in client.iter_messages(src, limit=SCAN_LIMIT):
            msgs.append(msg)
        msgs.reverse()

        pushed = 0
        changed = False
        for msg in msgs:
            addr = extract_addr(msg.message or "")
            if not addr:
                continue
            if addr in seen or addr in already:
                continue
            body = fmt_push(msg.message or "", addr)
            try:
                await client.send_message(tgt, body, parse_mode="html", link_preview=False)
                print("PUSHED: {} | {}".format(addr, title_line(msg.message or "")))
            except Exception as e:
                print("push failed {}: {}".format(addr, e))
                continue
            await asyncio.sleep(1.0)
            seen.add(addr)
            pushed += 1
            changed = True

        state["seen"] = sorted(seen)
        if changed:
            try:
                write_json_file(STATE_PATH, state, sha,
                                "sync onchain-alpha state from cloud [skip ci]")
                print("state written, seen={}".format(len(seen)))
            except Exception as e:
                print("state write failed: {}".format(e))
        print("done pushed={} seen={}".format(pushed, len(seen)))
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
