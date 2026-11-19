# -*- coding: utf-8 -*-
"""Empire SOL Trending 云端兜底（仅记录去重，不推送）。

接力逻辑:
- 本机在线（心跳新鲜）时云端直接跳过，不扫描、不写状态；
- 本机离线时云端接管：拉取源频道最近消息，把新出现的 SOL 合约并入共享
  去重集并回写 GitHub，推进游标；**不向任何群推送、不打开浏览器、不交易**；
- 本机开机后重新上线，以 GitHub 共享去重集为准继续去重，避免云端关机间隙
  出现过的合约被重复推送。

独立命名空间: empire_sol_trending_seen.json / _cursor.json / _heartbeat.txt
独立凭据: ESTR_TG_API_ID / ESTR_TG_API_HASH / ESTR_TG_STRING_SESSION（不复用其它任务）。

Env:
  ESTR_TG_API_ID / ESTR_TG_API_HASH / ESTR_TG_STRING_SESSION
  GITHUB_TOKEN（Actions 自动提供）
"""
import asyncio
import base64
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

API_ID = int(os.environ.get("ESTR_TG_API_ID", "0"))
API_HASH = os.environ.get("ESTR_TG_API_HASH", "")
STRING = os.environ.get("ESTR_TG_STRING_SESSION", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

GH_REPO = "qianjia2021-coder/gmgn-tg-monitor"
GH_API = "https://api.github.com"

SOURCE_USERNAME = "bhsoljupdatatrending"

GH_SEEN_PATH = "empire_sol_trending_seen.json"
GH_CURSOR_PATH = "empire_sol_trending_cursor.json"
GH_HEARTBEAT_PATH = "empire_sol_trending_heartbeat.txt"

# 心跳阈值 300s：本机心跳每 60s 一次，GitHub commit 有 40~70s 排队延迟
HEARTBEAT_STALE_SEC = 300
# 云端窗口放宽到 24 小时 + 游标双保险，覆盖关机间隙
SCAN_WINDOW_MIN = 24 * 60
SCAN_LIMIT = 1000

B58 = r"1-9A-HJ-NP-Za-km-z"
CA_RE = re.compile(r"CA:\s*([" + B58 + r"]{32,44})")
TOKEN_URL_RE = re.compile(r"gmgn\.ai/sol/token/(?:10Xboost_)?([" + B58 + r"]{32,44})")
PLAIN_RE = re.compile(r"\b([" + B58 + r"]{32,44})\b")
SOL_NATIVE = "So11111111111111111111111111111111111111112"
B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def log(msg):
    print("[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def valid_b58(addr: str) -> bool:
    if not (32 <= len(addr) <= 44) or addr == SOL_NATIVE:
        return False
    if any(c in addr for c in "0OIl"):
        return False
    return bool(PLAIN_RE.fullmatch(addr))


def extract_addrs(text: str) -> list:
    addrs = []
    for a in CA_RE.findall(text or ""):
        if valid_b58(a) and a not in addrs:
            addrs.append(a)
    for a in TOKEN_URL_RE.findall(text or ""):
        if valid_b58(a) and a not in addrs:
            addrs.append(a)
    if not addrs:
        for a in PLAIN_RE.findall(text or ""):
            if valid_b58(a) and a not in addrs:
                addrs.append(a)
    return addrs


def gh_headers():
    return {"Authorization": "token " + GITHUB_TOKEN,
            "Accept": "application/vnd.github+json",
            "User-Agent": "cloud-empire-sol-trending"}


def gh_read(path):
    try:
        req = urllib.request.Request(
            f"{GH_API}/repos/{GH_REPO}/contents/{path}", headers=gh_headers())
        with urllib.request.urlopen(req, timeout=25) as r:
            j = json.loads(r.read().decode())
        return base64.b64decode(j["content"]).decode(), j.get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        log(f"读取 {path} 失败: HTTP {e.code}")
        return None, None
    except Exception as e:
        log(f"读取 {path} 异常: {e}")
        return None, None


def gh_write(path, content, sha, msg):
    body = {"message": msg, "content": base64.b64encode(content.encode()).decode(),
            "branch": "main"}
    if sha:
        body["sha"] = sha
    for attempt in range(3):
        req = urllib.request.Request(
            f"{GH_API}/repos/{GH_REPO}/contents/{path}",
            data=json.dumps(body).encode(), method="PUT",
            headers={**gh_headers(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                log(f"写入 {path} OK (HTTP {r.status})")
                return True
        except urllib.error.HTTPError as e:
            if e.code == 409 and attempt < 2:
                _, sha = gh_read(path)
                body["sha"] = sha
                time.sleep(2)
                continue
            log(f"写入 {path} 失败: HTTP {e.code}")
            return False
        except Exception as e:
            log(f"写入 {path} 异常: {e}")
            time.sleep(2)
    return False


def local_is_online() -> bool:
    raw, _ = gh_read(GH_HEARTBEAT_PATH)
    if not raw:
        return False
    try:
        ts = raw.strip().rsplit(" ", 1)[-1]
        hb_time = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - hb_time).total_seconds()
        log(f"本机心跳年龄: {age:.0f}s (阈值 {HEARTBEAT_STALE_SEC}s)")
        return age < HEARTBEAT_STALE_SEC
    except Exception as e:
        log(f"心跳解析失败: {e}")
        return False


async def connect_with_retry():
    last = None
    for attempt in range(4):
        client = None
        try:
            client = TelegramClient(
                StringSession(STRING), API_ID, API_HASH,
                connection_retries=2, retry_delay=3, timeout=30, request_retries=2)
            await asyncio.wait_for(client.connect(), timeout=60)
            if not await asyncio.wait_for(client.is_user_authorized(), timeout=20):
                await client.disconnect()
                raise RuntimeError("会话未授权")
            return client
        except Exception as e:
            last = e
            log(f"连接尝试 {attempt+1} 失败: {type(e).__name__}: {str(e)[:120]}")
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(5 * (attempt + 1))
    raise RuntimeError("无法连接 Telegram: {}".format(last))


async def main():
    if not GITHUB_TOKEN:
        log("FATAL: GITHUB_TOKEN 未注入")
        sys.exit(1)

    if local_is_online():
        log("本机在线，本轮云端跳过")
        return

    log("本机离线，云端接管（仅记录去重，不推送）")

    seen = set()
    raw, sha_seen = gh_read(GH_SEEN_PATH)
    if raw:
        try:
            seen = set(json.loads(raw))
            log(f"已读取共享去重集: {len(seen)} 个")
        except Exception:
            log("去重集解析失败，按空集处理")

    cursor = 0
    raw_c, sha_cursor = gh_read(GH_CURSOR_PATH)
    if raw_c:
        try:
            cursor = int(json.loads(raw_c).get("last_msg_id", 0))
        except Exception:
            pass
    log(f"共享游标: msg_id > {cursor}")

    client = await connect_with_retry()
    try:
        me = await client.get_me()
        log(f"云端登录: @{getattr(me, 'username', '')} (id={me.id})")

        chat = await client.get_entity(SOURCE_USERNAME)
        log(f"监听频道: {getattr(chat, 'title', '?')}")

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=SCAN_WINDOW_MIN)
        scanned = 0
        recorded = 0
        max_id = cursor

        async for m in client.iter_messages(chat, limit=SCAN_LIMIT):
            scanned += 1
            if m.id <= cursor:
                break
            if m.date is None or m.date < cutoff:
                break
            if m.id > max_id:
                max_id = m.id
            for addr in extract_addrs(m.message or ""):
                if addr in seen:
                    continue
                seen.add(addr)  # 仅记录去重，不推送、不开浏览器、不交易
                recorded += 1

        log(f"扫描 {scanned} 条，新记录 {recorded} 个，去重集总数 {len(seen)}")

        gh_write(GH_SEEN_PATH,
                 json.dumps(sorted(seen), ensure_ascii=False, indent=1),
                 sha_seen, "cloud empire sol trending seen [skip ci]")
        if max_id > cursor:
            gh_write(GH_CURSOR_PATH,
                     json.dumps({"last_msg_id": max_id,
                                 "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                                indent=1),
                     sha_cursor, "cloud empire sol trending cursor [skip ci]")
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
