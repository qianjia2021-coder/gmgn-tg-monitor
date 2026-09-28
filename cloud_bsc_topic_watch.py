"""
Cloud watcher: BSC "BNB大厅" topic -> BSC push group.
Runs every 5 min via GitHub Actions.

Logic:
  1) Pull last ~60 messages in @web3_gxfc topic 7438, extract 0x BSC addresses.
  2) Skip anything already present in target group recent messages (dual-end dedupe
     against the local real-time listener) and anything already in cloud_bsc_cache.json.
  3) For each brand-new address: call GMGN openapi token info (bsc), push to BSC group.
  4) Persist pushed addresses back to cloud_bsc_cache.json.

Env:
  TG_API_ID, TG_API_HASH, TG_STRING_SESSION, GMGN_API_KEY
  GITHUB_TOKEN (auto-provided)
"""
import asyncio
import base64
import json
import os
import re
import sys
import time
import uuid

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import CheckChatInviteRequest

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
STRING_SESSION = os.environ["TG_STRING_SESSION"]
GMGN_KEY = os.environ["GMGN_API_KEY"]
GH_TOKEN = os.environ["GITHUB_TOKEN"]

REPO = "qianjia2021-coder/gmgn-tg-monitor"
CACHE_PATH = "cloud_bsc_cache.json"

SRC = "web3_gxfc"
TOPIC_ID = 7438
DST_INVITE = "0NCSpGbGf_MyNWU1"   # BSC推送群
PULL_LIMIT = 60

ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")
GMGN_INFO = "https://openapi.gmgn.ai/v1/token/info"
GH_API = "https://api.github.com"


def gh_headers():
    return {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "bsc-topic-watch",
    }


def read_cache():
    r = requests.get(f"{GH_API}/repos/{REPO}/contents/{CACHE_PATH}",
                     headers=gh_headers(), timeout=20)
    if r.status_code == 404:
        return {}, None
    r.raise_for_status()
    j = r.json()
    content = base64.b64decode(j["content"]).decode("utf-8")
    return json.loads(content), j["sha"]


def write_cache(cache, sha):
    data = json.dumps(cache, indent=2).encode("utf-8")
    body = {
        "message": "update cloud_bsc_cache",
        "content": base64.b64encode(data).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{REPO}/contents/{CACHE_PATH}",
                     headers=gh_headers(), json=body, timeout=20)
    r.raise_for_status()


def gmgn_token_info(addr):
    q = {
        "chain": "bsc", "address": addr,
        "timestamp": int(time.time()), "client_id": str(uuid.uuid4()),
    }
    headers = {"X-APIKEY": GMGN_KEY, "Content-Type": "application/json",
               "User-Agent": "bsc-topic-watch/1.0"}
    for attempt in range(3):
        try:
            r = requests.get(GMGN_INFO, params=q, headers=headers, timeout=20)
        except Exception as e:
            print(f"  gmgn request err: {e}")
            time.sleep(2)
            continue
        if r.status_code == 429:
            time.sleep(5)
            continue
        if r.status_code != 200:
            print(f"  gmgn http {r.status_code}: {r.text[:200]}")
            return None
        d = r.json()
        return d.get("data") or d
    return None


async def main():
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("FATAL: not authorized")
        sys.exit(1)
    me = await client.get_me()
    print(f"connected as @{me.username}")

    inv = await client(CheckChatInviteRequest(DST_INVITE))
    dst = getattr(inv, "chat", None) or inv.chats[0]
    print(f"src=@{SRC} topic={TOPIC_ID} dst={dst.title}")

    # 1) collect addresses from source topic
    found = {}
    async for msg in client.iter_messages(SRC, reply_to=TOPIC_ID, limit=PULL_LIMIT):
        text = msg.message or ""
        for m in ADDR_RE.finditer(text):
            a = m.group(0).lower()
            if a not in found:
                found[a] = msg.id
    print(f"source topic unique addrs: {len(found)}")

    # 2) dedupe: anything already in target group recent messages
    already = set()
    async for msg in client.iter_messages(dst, limit=40):
        if not msg.message:
            continue
        for m in ADDR_RE.finditer(msg.message):
            already.add(m.group(0).lower())
    print(f"already-sent in dst: {len(already)}")

    # 3) load cloud cache
    cache, cache_sha = read_cache()
    print(f"cloud cache entries: {len(cache)}")

    now = int(time.time())
    pushed = 0
    cache_changed = False
    for addr, msg_id in found.items():
        if addr in already or addr in cache:
            continue
        info = gmgn_token_info(addr) or {}
        sym = info.get("symbol") or ""
        name = info.get("name") or ""
        mc = 0
        price = info.get("price") or {}
        if isinstance(price, dict):
            mc = round(float(price.get("market_cap") or 0))
        if not mc:
            mc = round(float(info.get("market_cap") or 0))
        liq = round(float(info.get("liquidity") or 0))
        holders = info.get("holder_count") or 0
        stat = info.get("wallet_tags_stat") or {}
        kol = int(stat.get("renowned_wallets") or 0)
        link = f"https://t.me/{SRC}/{TOPIC_ID}/{msg_id}"
        text = (
            f"🟢 <b>BNB大厅新合约（云端）</b>  #{sym} {name}\n"
            f"市值约 ${mc:,} | 流动性 ${liq:,} | 持有人 {holders}\n"
            f"KOL {kol} 个（参考）\n"
            f"GMGN: https://gmgn.ai/bsc/token/{addr}\n"
            f"原消息: {link}\n"
            f"<code>{addr}</code>"
        )
        try:
            await client.send_message(dst, text, parse_mode="html", link_preview=False)
            pushed += 1
            print(f"  PUSHED #{sym} {addr}")
            cache[addr] = now
            cache_changed = True
            await asyncio.sleep(1.5)
        except Exception as e:
            print(f"  push fail {addr}: {e}")

    if cache_changed:
        try:
            write_cache(cache, cache_sha)
            print("cloud cache written")
        except Exception as e:
            print(f"cache write failed: {e}")

    await client.disconnect()
    print(f"done pushed={pushed}")


if __name__ == "__main__":
    asyncio.run(main())
