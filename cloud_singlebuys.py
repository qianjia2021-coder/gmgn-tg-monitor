# -*- coding: utf-8 -*-
"""云端兜底：每5分钟跑一次，扫 SINGLEBUYS 话题最近5分钟新合约，推送到目标群。
GitHub Actions 环境变量：
  SB_TG_API_ID, SB_TG_API_HASH, SB_TG_STRING_SESSION
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

API_ID = int(os.environ["SB_TG_API_ID"])
API_HASH = os.environ["SB_TG_API_HASH"]
STRING = os.environ["SB_TG_STRING_SESSION"]

CHAT_ID = -1003959309696
TOPIC_ROOT = 1499509
TARGET_INVITE_HASH = "QfV7aMQ05B02ODE9"
SEEN_FILE = "seen.json"
SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{43,44}\b")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


async def main():
    client = TelegramClient(StringSession(STRING), API_ID, API_HASH)
    await client.connect()
    me = await client.get_me()
    log(f"已登录: {me.username}")

    chat = await client.get_entity(CHAT_ID)
    log(f"监听群: {chat.title}")

    info = await client(CheckChatInviteRequest(TARGET_INVITE_HASH))
    target = getattr(info, "chat", None)
    if target is None:
        res = await client(ImportChatInviteRequest(TARGET_INVITE_HASH))
        target = res.chats[0]
    log(f"推送目标: {target.title}")

    seen = set()
    if os.path.exists(SEEN_FILE):
        seen = set(json.load(open(SEEN_FILE)))

    bj = timezone(timedelta(hours=8))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=6)

    pushed = 0
    async for m in client.iter_messages(chat, reply_to=TOPIC_ROOT, limit=100):
        if not m.date or m.date < cutoff:
            break
        text = m.message or ""
        for addr in SOL_RE.findall(text):
            if addr in seen:
                continue
            seen.add(addr)
            now_bj = datetime.now(bj).strftime("%Y-%m-%d %H:%M:%S")
            src_bj = m.date.astimezone(bj).strftime("%Y-%m-%d %H:%M:%S")
            msg = (
                f"SINGLEBUYS 新合约\n"
                f"出现时间: {src_bj} (北京时间)\n"
                f"推送时间: {now_bj} (北京时间)\n"
                f"\n"
                f"<code>{addr}</code>"
            )
            await client.send_message(target, msg, parse_mode="html")
            log(f"已推送: {addr}")
            pushed += 1

    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f)
    log(f"本轮推送 {pushed} 个，seen 总数 {len(seen)}")
    await client.disconnect()


asyncio.run(main())
