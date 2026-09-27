"""
Cloud monitor for gmgn100x fresh-token alert.
Runs once per GitHub Actions tick (every 5 min). No proxy needed.

Env:
  TG_API_ID, TG_API_HASH, TG_STRING_SESSION
  GMGN_API_KEY
  SRC_CHANNEL (default: gmgn100x)
  DST_INVITE_HASH
  MAX_AGE_SEC (default: 600)
"""
import asyncio
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl
from telethon.tl.functions.messages import CheckChatInviteRequest

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
STRING_SESSION = os.environ["TG_STRING_SESSION"]
GMGN_KEY = os.environ["GMGN_API_KEY"]

SRC = os.environ.get("SRC_CHANNEL", "gmgn100x")
DST_INVITE = os.environ["DST_INVITE_HASH"]
MAX_AGE = int(os.environ.get("MAX_AGE_SEC", "600"))
PULL_LIMIT = 60

TOKEN_URL_RE = re.compile(r"gmgn\.ai/sol/token/10Xboost_([A-Za-z0-9]+)")
SYM_RE = re.compile(r"\[([^\[\]]{1,12})\]")
BUY_RE = re.compile(r"Buy\s+([0-9.,]+)\s*(USDC|SOL|WSOL)\s+([0-9.,KMkMB]+)\s*\[")
GMGN_INFO = "https://openapi.gmgn.ai/v1/token/info"
GMGN_BASE = "https://gmgn.ai/sol/token/"


def extract_token(msg):
    if not msg.entities:
        return None
    for ent in msg.entities:
        if isinstance(ent, MessageEntityTextUrl) and ent.url:
            m = TOKEN_URL_RE.search(ent.url)
            if m:
                return m.group(1)
    return None


def parse_msg(msg):
    txt = msg.message or ""
    lines = [l for l in txt.splitlines() if l.strip()]
    head = lines[0] if lines else ""
    body = lines[1] if len(lines) > 1 else ""
    m_trader = re.match(r"\[([^\]]+)\]", head)
    trader = m_trader.group(1) if m_trader else ""
    m_sym = SYM_RE.search(body)
    sym = m_sym.group(1) if m_sym else ""
    buy = ""
    m_buy = BUY_RE.search(body)
    if m_buy:
        buy = f"{m_buy.group(1)} {m_buy.group(2)}"
    return trader, sym, buy


def gmgn_token_info(addr):
    q = {
        "chain": "sol", "address": addr,
        "timestamp": int(time.time()), "client_id": str(uuid.uuid4()),
    }
    headers = {"X-APIKEY": GMGN_KEY, "Content-Type": "application/json",
               "User-Agent": "gmgn-monitor/1.0"}
    r = requests.get(GMGN_INFO, params=q, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json().get("data") or r.json()


async def main():
    proxy = None
    if os.environ.get("LOCAL_PROXY") == "1":
        proxy = ('socks5', '127.0.0.1', 7897, True)
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH, proxy=proxy)
    await client.connect()
    if not await client.is_user_authorized():
        print("FATAL: not authorized")
        sys.exit(1)
    me = await client.get_me()
    print(f"connected as @{me.username}")

    inv = await client(CheckChatInviteRequest(DST_INVITE))
    dst = getattr(inv, "chat", None) or inv.chats[0]
    print(f"src=@{SRC}  dst={dst.title}  max_age={MAX_AGE}s")

    # 1) collect tokens from source
    tokens = {}
    async for msg in client.iter_messages(SRC, limit=PULL_LIMIT):
        addr = extract_token(msg)
        if not addr or addr.lower().endswith("pump"):
            continue
        trader, sym, buy = parse_msg(msg)
        rec = tokens.get(addr)
        if rec is None:
            tokens[addr] = {"trader": trader, "sym": sym, "buy": buy, "hits": 1}
        else:
            rec["hits"] += 1
            if not rec["sym"] and sym:
                rec["sym"] = sym
    print(f"source unique tokens: {len(tokens)}")

    # 2) dedupe against already-sent in target (last 30 msgs)
    already = set()
    async for msg in client.iter_messages(dst, limit=30):
        if not msg.message:
            continue
        for m in re.findall(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", msg.message):
            already.add(m)
    print(f"already-sent in dst: {len(already)}")

    now = int(time.time())
    pushed = 0
    for addr, rec in tokens.items():
        if addr in already:
            continue
        try:
            info = gmgn_token_info(addr)
        except Exception as e:
            print(f"  gmgn err {addr[:8]}: {e}")
            continue
        ct = int(info.get("creation_timestamp") or 0)
        if ct <= 0:
            continue
        age = now - ct
        print(f"  ${info.get('symbol','?'):<10} age={age}s  lp=${info.get('liquidity',0)}")
        if age > MAX_AGE:
            continue
        text = (
            f"🟢 <b>@gmgn100x · SOL · {age}s</b>\n"
            f"代币: <b>${info.get('symbol') or rec['sym'] or '?'}</b>\n"
            f"合约: <code>{addr}</code>\n"
            f"流动性: ${float(info.get('liquidity',0)):.0f}  |  持仓地址: {info.get('holder_count',0)}\n"
            f"Launchpad: {info.get('launchpad') or '-'}\n"
            f"源信号: {rec['buy']} by {rec['trader']}\n"
            f"Chart: {GMGN_BASE}{addr}"
        )
        await client.send_message(dst, text, parse_mode="html", link_preview=False)
        pushed += 1
        print(f"  ✅ PUSHED {addr} age={age}s")
        await asyncio.sleep(1.5)

    await client.disconnect()
    print(f"done pushed={pushed}")


if __name__ == "__main__":
    asyncio.run(main())
