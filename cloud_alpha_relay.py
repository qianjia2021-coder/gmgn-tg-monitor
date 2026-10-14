#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OnChain Alpha Trench 合约中继 - 云端版（GitHub Actions）
================================================================
本地关机时由 GitHub Actions 每 5 分钟接管：
  1. 读 heartbeat.txt：若本地 4 分钟内有心跳 -> 本地在线，直接退出（双端互斥）
  2. 扫源群 @onchainalphatrench 最近 60 条消息，提取合约地址
  3. 读云端 cloud_alpha_seen.json；新合约 -> 推到目标群(chat_id=5194908956)
  4. 写回 cloud_alpha_seen.json

Secrets（在仓库 Settings -> Secrets 配置）:
  TG_STRING_SESSION  telethon StringSession（与本地同一账号）
  TG_API_ID          int
  TG_API_HASH        str
  GITHUB_TOKEN       Actions 自动提供
"""
import asyncio
import base64
import json
import os
import re
import time

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import InputPeerChat

REPO = "qianjia2021-coder/gmgn-tg-monitor"
SEEN_PATH = "cloud_alpha_seen.json"
HEARTBEAT_PATH = "alpha_heartbeat.txt"
GH_API = "https://api.github.com"

SOURCE_GROUP = "onchainalphatrench"
TARGET_PEER = InputPeerChat(5194908956)   # BSC推送群（基础群）
SCAN_LIMIT = 60                           # 每次扫源群最近多少条
HEARTBEAT_FRESH_SECONDS = 4 * 60          # 4 分钟内有心跳视为本地在线

ADDR_RE = re.compile(r"\b(?:[1-9A-HJ-NP-Za-km-z]{32,44}|0x[a-fA-F0-9]{40})\b")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S") + " " + str(msg), flush=True)


def gh_headers():
    return {
        "Authorization": "token " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
        "User-Agent": "alpha-relay-cloud",
    }


def read_json_file(path):
    r = requests.get(f"{GH_API}/repos/{REPO}/contents/{path}",
                     headers=gh_headers(), timeout=20)
    if r.status_code == 404:
        return {}, None
    r.raise_for_status()
    j = r.json()
    content = base64.b64decode(j["content"]).decode("utf-8")
    return json.loads(content), j["sha"]


def write_json_file(path, data, sha, msg):
    payload = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    body = {"message": msg, "content": base64.b64encode(payload).decode(),
            "branch": "main"}
    if sha:
        body["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{REPO}/contents/{path}",
                     headers=gh_headers(), json=body, timeout=30)
    r.raise_for_status()


def read_text_file(path):
    r = requests.get(f"{GH_API}/repos/{REPO}/contents/{path}",
                     headers=gh_headers(), timeout=20)
    if r.status_code == 404:
        return "", None
    r.raise_for_status()
    j = r.json()
    return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]


def local_online():
    """读 heartbeat.txt；最近 4 分钟内有更新视为本地在线。"""
    try:
        text, _ = read_text_file(HEARTBEAT_PATH)
        text = text.strip()
        if not text:
            return False, "no heartbeat"
        # 格式: "2026-10-14 10:00:00"
        ts = time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
        age = time.time() - ts
        if age < HEARTBEAT_FRESH_SECONDS:
            return True, f"heartbeat age {int(age)}s (< {HEARTBEAT_FRESH_SECONDS}s)"
        return False, f"heartbeat stale age {int(age)}s"
    except Exception as e:
        return False, f"heartbeat check err: {e}"


def extract_addrs(text):
    out = set()
    if not text:
        return out
    for m in ADDR_RE.findall(text):
        a = m.lower()
        if a.startswith("0x"):
            if len(a) == 42:
                out.add(a)
        else:
            if 32 <= len(a) <= 44:
                out.add(a)
    return out


async def run():
    online, reason = local_online()
    log(f"local online check: {online} ({reason})")
    if online:
        log("本地在线，云端不接管，退出")
        return 0

    # 读云端 seen
    seen_data, seen_sha = read_json_file(SEEN_PATH)
    if not isinstance(seen_data, dict):
        seen_data = {}
    seen = set(a.lower() for a in seen_data.get("seen", []))
    log(f"云端 seen 数量: {len(seen)}")

    # 连接 TG
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    string_session = os.environ["TG_STRING_SESSION"]
    client = TelegramClient(StringSession(string_session), api_id, api_hash)
    pushed = 0
    try:
        await client.connect()
        if not await client.is_user_authorized():
            log("StringSession 未授权")
            return 1
        peer = await client.get_entity(SOURCE_GROUP)
        log(f"源群解析: {getattr(peer,'title',SOURCE_GROUP)}")

        async for msg in client.iter_messages(peer, limit=SCAN_LIMIT):
            text = msg.message or ""
            if not text:
                continue
            addrs = extract_addrs(text)
            new_addrs = addrs - seen
            if not new_addrs:
                continue
            # 转发
            # 只取第一行非空行作为标题（详细分析全部扔掉）
            first_line = ""
            for line in text.splitlines():
                line = line.strip()
                if line:
                    first_line = line
                    break
            header = ADDR_RE.sub("", first_line).strip()
            lines = ["📡 <b>OnChain Alpha Trench 新合约</b>", ""]
            if header:
                lines.append(header)
                lines.append("")
            for a in sorted(addrs):
                lines.append(f"<code>{a}</code>")
            out = "\n".join(lines)
            try:
                await client.send_message(TARGET_PEER, out, parse_mode="html",
                                          link_preview=False)
                log(f"已转发新合约 {len(new_addrs)} 个: {sorted(new_addrs)[:3]}")
                pushed += 1
            except Exception as e:
                log(f"转发失败: {e}")
                continue
            seen |= addrs
    finally:
        await client.disconnect()

    # 写回 seen
    seen_data["seen"] = sorted(seen)
    seen_data["last_cloud_push"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_json_file(SEEN_PATH, seen_data, seen_sha,
                    f"alpha relay cloud: pushed {pushed} msgs, seen {len(seen)} [skip ci]")
    log(f"本轮完成: pushed={pushed}, seen={len(seen)}")
    return 0


if __name__ == "__main__":
    sys_rc = asyncio.run(run())
    raise SystemExit(sys_rc or 0)
