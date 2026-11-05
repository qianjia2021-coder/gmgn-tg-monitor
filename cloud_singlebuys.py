# -*- coding: utf-8 -*-
"""云端 SINGLEBUYS 兜底监控（重写版，2026-11-05）。

原版（cloud_singlebuys.py，82 行）的四个断点，均已实测确认：

1) **凭据与本机同源** —— 部署脚本 deploy_singlebuys_cloud.py 把本机的
   probe_string.txt 直接塞进 SB_TG_STRING_SESSION secret。
   本机在线时云端若接管，两者用同一份 auth_key 从不同出口 IP 上线
   （本机走 Clash 代理，云端是 GitHub runner 直连），
   Telegram 判定 AuthKeyDuplicatedError 并永久吊销该 auth_key。
   这正是 2026-11-04 生产事故的同款陷阱，只是这次会打在本机监控头上。
   -> 修复：云端必须使用**独立凭据**（cloud_string.txt），由
      login_cloud_session.py 生成，绝不与本机共用。

2) **seen 状态不互通** —— 原版把 seen 写在 runner 的本地文件 seen.json，
   runner 每次都是全新容器，文件不保留 → 每轮 seen 都是空的，
   会把历史合约全部重推一遍。
   线上 seen.json 里 545 条全是 EVM 地址（0x...），
   与本机 1939 条 Solana 地址完全不是一回事 —— 说明那是另一个任务的残留。
   -> 修复：seen 存到 GitHub 仓库的单一文件，双方共用同一份，
      本机定期推送，云端读取并回写。

3) **扫描窗口太窄** —— 原版 cutoff 只看最近 6 分钟、limit 100 条。
   云端本身 3~5 小时才跑一次，关机超过 6 分钟的合约永久漏掉。
   -> 修复：窗口放大到 24 小时、limit 提高到 1000 条，
      并用「上次处理到的 msg_id」做增量游标，双保险。

4) **无代理环境下 Telegram 连不通的兼容性** —— GitHub runner 在部分
   IP 段访问 Telegram DC 会超时，需要重试与更宽容的连接参数。
   -> 修复：加连接重试、更长超时。

Env:
  SB_TG_API_ID / SB_TG_API_HASH / SB_TG_STRING_SESSION  （独立云端凭据）
  GITHUB_TOKEN（Actions 自动提供）
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import (
    CheckChatInviteRequest, ImportChatInviteRequest)

API_ID = int(os.environ["SB_TG_API_ID"])
API_HASH = os.environ["SB_TG_API_HASH"]
STRING = os.environ["SB_TG_STRING_SESSION"]

GH_REPO = "qianjia2021-coder/gmgn-tg-monitor"
GH_API = "https://api.github.com"

CHAT_ID = -1003959309696      # SOLANA EMPIRE
TOPIC_ROOT = 1499509          # SINGLEBUYS
TARGET_INVITE_HASH = "QfV7aMQ05B02ODE9"

# GitHub 上的共享状态文件（本机与云端共用同一份）
GH_SEEN_PATH = "singlebuys_seen.json"
GH_CURSOR_PATH = "singlebuys_cursor.json"

# 扫描窗口：云端 3~5 小时才跑一次，窗口必须够大才能覆盖关机间隙
SCAN_WINDOW_MIN = 24 * 60
SCAN_LIMIT = 1000

# 严格 Solana 地址：base58 且解码后 32 字节
SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{43,44}\b")
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SOL_NATIVE = "So11111111111111111111111111111111111111112"


def log(msg):
    print("[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg),
          flush=True)


def b58_decode(s):
    n = 0
    for ch in s:
        i = B58.find(ch)
        if i < 0:
            return None
        n = n * 58 + i
    pad = len(s) - len(s.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + body


def is_valid_sol(s):
    """严格校验 Solana 地址：长度合规 + base58 可解码 + 解码后 32 字节 + 非原生 SOL。

    原版只用了长度 43-44 的正则，会把 EVM 地址等噪声也放进来。
    """
    if not (43 <= len(s) <= 44) or s == SOL_NATIVE:
        return False
    raw = b58_decode(s)
    return raw is not None and len(raw) == 32


def gh_headers():
    return {"Authorization": "token " + os.environ["GITHUB_TOKEN"],
            "Accept": "application/vnd.github+json",
            "User-Agent": "cloud-singlebuys"}


def gh_read(path):
    """读 GitHub 上的文件。返回 (内容, sha)，不存在返回 (None, None)。"""
    try:
        req = urllib.request.Request(
            "{}/repos/{}/contents/{}".format(GH_API, GH_REPO, path),
            headers=gh_headers())
        with urllib.request.urlopen(req, timeout=25) as r:
            j = json.loads(r.read().decode())
        return base64.b64decode(j["content"]).decode(), j.get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        log("读取 {} 失败: HTTP {}".format(path, e.code))
        return None, None
    except Exception as e:
        log("读取 {} 异常: {}".format(path, e))
        return None, None


def gh_write(path, content, sha, msg):
    """写 GitHub 上的文件。"""
    body = {"message": msg,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": "main"}
    if sha:
        body["sha"] = sha
    for attempt in range(3):
        req = urllib.request.Request(
            "{}/repos/{}/contents/{}".format(GH_API, GH_REPO, path),
            data=json.dumps(body).encode(), method="PUT",
            headers={**gh_headers(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                log("已写入 {} (HTTP {})".format(path, r.status))
                return True
        except urllib.error.HTTPError as e:
            if e.code == 409 and attempt < 2:
                # sha 冲突：重取最新 sha 再写
                _, sha = gh_read(path)
                body["sha"] = sha
                time.sleep(2)
                continue
            log("写入 {} 失败: HTTP {}".format(path, e.code))
            return False
        except Exception as e:
            log("写入 {} 异常: {}".format(path, e))
            time.sleep(2)
    return False


async def resolve_target(client):
    """解析推送目标群。"""
    try:
        res = await client(CheckChatInviteRequest(hash=TARGET_INVITE_HASH))
        chat = getattr(res, "chat", None)
        if chat is not None:
            return chat
    except Exception as e:
        log("CheckChatInvite 失败: {}: {}".format(type(e).__name__, e))
    try:
        upd = await client(ImportChatInviteRequest(hash=TARGET_INVITE_HASH))
        for c in getattr(upd, "chats", None) or []:
            return c
    except Exception as e:
        log("ImportChatInvite 失败: {}: {}".format(type(e).__name__, e))
    return None


async def connect_with_retry():
    """连接 Telegram。GitHub runner 到 DC 的链路不稳，需要多次尝试。"""
    last = None
    for attempt in range(4):
        client = None
        try:
            client = TelegramClient(
                StringSession(STRING), API_ID, API_HASH,
                connection_retries=2, retry_delay=3, timeout=30,
                request_retries=2)
            await asyncio.wait_for(client.connect(), timeout=60)
            if not await asyncio.wait_for(client.is_user_authorized(), timeout=30):
                await client.disconnect()
                raise RuntimeError("会话未授权")
            return client
        except Exception as e:
            last = e
            log("连接尝试 {} 失败: {}: {}".format(
                attempt + 1, type(e).__name__, str(e)[:120]))
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(5 * (attempt + 1))
    raise RuntimeError("无法连接 Telegram: {}".format(last))


async def main():
    # 读共享状态
    seen = set()
    raw, sha_seen = gh_read(GH_SEEN_PATH)
    if raw:
        try:
            seen = set(json.loads(raw))
            log("已读取共享 seen: {} 个".format(len(seen)))
        except Exception:
            log("seen 解析失败，按空集处理")

    cursor = 0
    raw_c, sha_cursor = gh_read(GH_CURSOR_PATH)
    if raw_c:
        try:
            cursor = int(json.loads(raw_c).get("last_msg_id", 0))
            log("已读取游标: msg_id > {}".format(cursor))
        except Exception:
            pass

    client = await connect_with_retry()
    try:
        me = await client.get_me()
        log("已登录 TG: {} (id={})".format(
            getattr(me, "username", "") or getattr(me, "first_name", ""), me.id))
        log("凭据指纹: {}".format(
            hashlib.sha256(bytes(client.session.auth_key.key)).hexdigest()[:32]))

        try:
            chat = await client.get_entity(CHAT_ID)
            log("监听群: {}".format(getattr(chat, "title", CHAT_ID)))
        except Exception as e:
            log("群访问失败（云端账号可能不在该群）: {}: {}".format(
                type(e).__name__, e))
            return

        target = await resolve_target(client)
        if target is None:
            log("!! 推送目标解析失败，本轮不推送")
        else:
            log("推送目标: {}".format(getattr(target, "title", "?")))

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=SCAN_WINDOW_MIN)
        bj = timezone(timedelta(hours=8))
        pushed = 0
        scanned = 0
        max_id = cursor

        async for m in client.iter_messages(chat, reply_to=TOPIC_ROOT,
                                            limit=SCAN_LIMIT):
            scanned += 1
            if m.id <= cursor:
                # 已处理过（增量游标），跳过
                continue
            if m.date is None or m.date < cutoff:
                # 超出时间窗口，剩下的更旧，直接停
                break
            text = m.message or ""
            for cand in SOL_RE.findall(text):
                if not is_valid_sol(cand):
                    continue
                if cand in seen:
                    continue
                seen.add(cand)
                if m.id > max_id:
                    max_id = m.id
                if target is None:
                    continue
                now_bj = datetime.now(bj).strftime("%Y-%m-%d %H:%M:%S")
                src_bj = m.date.astimezone(bj).strftime("%Y-%m-%d %H:%M:%S")
                msg = ("SINGLEBUYS 新合约\n"
                       "出现时间: {} (北京时间)\n"
                       "推送时间: {} (北京时间)\n"
                       "\n"
                       "<code>{}</code>").format(src_bj, now_bj, cand)
                try:
                    await client.send_message(target, msg, parse_mode="html")
                    log("  已推送: {}".format(cand))
                    pushed += 1
                except Exception as e:
                    log("  推送失败 {}: {}".format(cand, e))
                await asyncio.sleep(1)

        log("本轮扫描 {} 条消息，推送 {} 个合约，seen 总数 {}".format(
            scanned, pushed, len(seen)))

        # 回写共享状态
        gh_write(GH_SEEN_PATH,
                 json.dumps(sorted(seen), ensure_ascii=False, indent=1),
                 sha_seen, "cloud singlebuys seen [skip ci]")
        if max_id > cursor:
            gh_write(GH_CURSOR_PATH,
                     json.dumps({"last_msg_id": max_id,
                                 "ts": datetime.now(timezone.utc).strftime(
                                     "%Y-%m-%dT%H:%M:%SZ")}, indent=1),
                     sha_cursor, "cloud singlebuys cursor [skip ci]")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    log("done")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        log("FATAL: {}: {}".format(type(e).__name__, str(e)[:300]))
        sys.exit(1)
