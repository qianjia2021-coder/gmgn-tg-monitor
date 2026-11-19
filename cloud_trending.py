# -*- coding: utf-8 -*-
"""Empire SOL Trending 云端兜底（仅记录去重，不推送）。

接力逻辑:
- 本机在线（心跳新鲜）时云端直接跳过，不扫描、不写状态；
- 本机离线时云端接管：拉取源频道最近消息，把新出现的 SOL 合约并入共享
  去重集并回写 GitHub，推进游标；不向任何群推送、不打开浏览器、不交易；
- 本机开机后重新上线，以 GitHub 共享去重集为准继续去重，避免云端关机间隙
  出现过的合约被重复推送。

独立命名空间: empire_sol_trending_seen.json / _cursor.json / _heartbeat.txt
独立凭据: ESTR_TG_API_ID / ESTR_TG_API_HASH / ESTR_TG_STRING_SESSION（不复用其它任务）。

去重规则、地址提取与 GitHub 读写复用 estr_common，保证与本机口径一致。

Env:
  ESTR_TG_API_ID / ESTR_TG_API_HASH / ESTR_TG_STRING_SESSION
  GITHUB_TOKEN（Actions 自动提供）
  ESTR_GH_REPO（可选，默认 qianjia2021-coder/gmgn-tg-monitor）
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient
from telethon.sessions import StringSession

from estr_common import GitHubContents, extract_addrs, heartbeat_age_seconds

API_ID = int(os.environ.get("ESTR_TG_API_ID", "0"))
API_HASH = os.environ.get("ESTR_TG_API_HASH", "")
STRING = os.environ.get("ESTR_TG_STRING_SESSION", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("ESTR_GH_REPO", "qianjia2021-coder/gmgn-tg-monitor")

SOURCE_USERNAME = "bhsoljupdatatrending"

GH_SEEN_PATH = "empire_sol_trending_seen.json"
GH_CURSOR_PATH = "empire_sol_trending_cursor.json"
GH_HEARTBEAT_PATH = "empire_sol_trending_heartbeat.txt"

# 心跳阈值 300s：本机心跳每 60s 一次，GitHub commit 有 40~70s 排队延迟
HEARTBEAT_STALE_SEC = 300
# 云端窗口放宽到 24 小时 + 游标双保险，覆盖关机间隙
SCAN_WINDOW_MIN = 24 * 60
SCAN_LIMIT = 1000

def log(msg):
    """输出带时间戳的日志行。"""
    print("[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


gh = GitHubContents(GH_REPO, GITHUB_TOKEN, ua="cloud-empire-sol-trending", log=log)


def local_is_online() -> bool:
    """依据心跳文件内容判定本机是否在线。"""
    raw, _ = gh.read(GH_HEARTBEAT_PATH)
    age = heartbeat_age_seconds(raw)
    if age is None:
        log("心跳文件缺失或解析失败 → 按离线处理")
        return False
    log(f"本机心跳年龄: {age:.0f}s (阈值 {HEARTBEAT_STALE_SEC}s)")
    return age < HEARTBEAT_STALE_SEC


async def connect_with_retry():
    """带退避重试的客户端连接。"""
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
        except Exception as error:
            last = error
            log(f"连接尝试 {attempt+1} 失败: {type(error).__name__}: {str(error)[:120]}")
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(5 * (attempt + 1))
    raise RuntimeError("无法连接 Telegram: {}".format(last))


async def main():
    """云端兜底主流程。"""
    if not GITHUB_TOKEN:
        log("FATAL: GITHUB_TOKEN 未注入")
        sys.exit(1)

    if local_is_online():
        log("本机在线，本轮云端跳过")
        return

    log("本机离线，云端接管（仅记录去重，不推送）")

    seen = set()
    raw, _ = gh.read(GH_SEEN_PATH)
    if raw:
        try:
            seen = set(json.loads(raw))
            log(f"已读取共享去重集: {len(seen)} 个")
        except Exception:
            log("去重集解析失败，按空集处理")

    cursor = 0
    raw_c, _ = gh.read(GH_CURSOR_PATH)
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

        async for message in client.iter_messages(chat, limit=SCAN_LIMIT):
            scanned += 1
            if message.id <= cursor:
                break
            if message.date is None or message.date < cutoff:
                break
            if message.id > max_id:
                max_id = message.id
            for addr in extract_addrs(message):
                if addr in seen:
                    continue
                seen.add(addr)  # 仅记录去重，不推送、不开浏览器、不交易
                recorded += 1

        log(f"扫描 {scanned} 条，新记录 {recorded} 个，去重集总数 {len(seen)}")

        ok_seen = gh.write(
            GH_SEEN_PATH,
            json.dumps(sorted(seen), ensure_ascii=False, indent=1),
            "cloud empire sol trending seen [skip ci]")
        if not ok_seen:
            log("警告: 去重集写入失败")
        if max_id > cursor:
            ok_cursor = gh.write(
                GH_CURSOR_PATH,
                json.dumps(
                    {"last_msg_id": max_id,
                     "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                    indent=1),
                "cloud empire sol trending cursor [skip ci]")
            if not ok_cursor:
                log("警告: 游标写入失败")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    log("done")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        log("FATAL: {}: {}".format(type(error).__name__, str(error)[:300]))
        sys.exit(1)
