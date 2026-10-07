#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Empire BSC Smart Money Buys 监控 - 云端版（GitHub Actions，每 5 分钟）
=====================================================================
- 扫描 Empire BSC 🧠 Smart Money Buys 最近消息，发现新合约即触发
- 扫描 debot_watcher_14_bot 最近消息，里面的合约全部拉黑
- 状态存 GitHub repo（cloud_empire_state.json），与本地双向合并
- 触发：新合约 → 发 TG 推送 + dbotx 买 0.1 BNB 止盈50%
- 云端不开浏览器（debot.ai 标签仅本地开）
"""
import asyncio, base64, json, os, re, time
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.environ.get("TG_API_ID", "39227045"))
API_HASH = os.environ.get("TG_API_HASH", "cd451ed24a3226f46bfd730ff4d8b5dd")
STRING_SESSION = os.environ["TG_STRING_SESSION"]

# 目标群组：Empire BSC 🧠 Smart Money Buys
EMPIRE_GROUP = "empirebscsmartmoney"
# debot_watcher_14_bot username（用 username 获取，避免云端 access_hash 问题）
WATCHER_BOT_USERNAME = "debot_watcher_14_bot"
# 推送群：BSC推送群
PUSH_CHAT_ID = int(os.environ.get("TG_PUSH_CHAT_ID", "-5194908956"))

SCAN_LIMIT = 30   # 每群扫最近 N 条
WINDOW_SEC = 1800  # 只处理最近 30 分钟内的消息

# GitHub 状态同步
REPO = "qianjia2021-coder/gmgn-tg-monitor"
STATE_PATH = "cloud_empire_state.json"
GH_API = "https://api.github.com"

# BSC 合约地址正则
ADDR_RE = re.compile(r"(?<![a-fA-F0-9])0x[a-fA-F0-9]{40}(?![a-fA-F0-9])")
ZERO = "0x0000000000000000000000000000000000000000"
SKIP = {ZERO, "0x10ed43c718714eb63d5aa57b78b54704e256024e",
        "0xca143ce32fe787f7f3def1387fb428719f23447"}

# dbotx 模拟器
DBOTX_SWAP_URL = "https://api-bot-v1.dbotx.com/simulator/sim_swap_order"
DBOTX_API_KEY = os.environ.get("DBOTX_API_KEY", "b6u3i891nbhigvwyqhj5h1o89d34wihq")
DBOTX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
BUY_AMOUNT = 0.1
STOP_EARN = 0.5
SLIPPAGE = 0.5

client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)


def gh_headers():
    return {"Authorization": "token " + os.environ["GITHUB_TOKEN"],
            "Accept": "application/vnd.github+json", "User-Agent": "empire-smartmoney-monitor"}


def read_state():
    r = requests.get(f"{GH_API}/repos/{REPO}/contents/{STATE_PATH}", headers=gh_headers(), timeout=20)
    if r.status_code == 404:
        return {"bought": {}, "contract_blacklist": {}}, None
    r.raise_for_status()
    j = r.json()
    return json.loads(base64.b64decode(j["content"]).decode()), j["sha"]


def write_state(st, sha):
    payload = json.dumps(st, ensure_ascii=False).encode("utf-8")
    body = {"message": "sync empire smartmoney state [skip ci]",
            "content": base64.b64encode(payload).decode(), "branch": "main"}
    if sha: body["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{REPO}/contents/{STATE_PATH}",
                     headers=gh_headers(), json=body, timeout=30)
    r.raise_for_status()


def extract_addrs(text):
    if not text: return []
    out = []
    for a in ADDR_RE.findall(text):
        al = a.lower()
        if al in SKIP or al in out: continue
        out.append(al)
    return out


def dbotx_buy(contract):
    body = {"chain": "bsc", "pair": contract, "walletId": "", "type": "buy",
            "amountOrPercent": BUY_AMOUNT, "stopEarnPercent": STOP_EARN, "stopLossPercent": None,
            "priorityFee": "", "gasFeeDelta": 5, "maxFeePerGas": 100, "slippage": SLIPPAGE}
    r = requests.post(DBOTX_SWAP_URL, json=body,
                      headers={"X-API-KEY": DBOTX_API_KEY, "Content-Type": "application/json",
                               "accept": "application/json", "User-Agent": DBOTX_UA,
                               "Origin": "https://dbotx.com", "Referer": "https://dbotx.com/"}, timeout=30)
    try:
        d = r.json()
        if d.get("err") is False: return True, d.get("res", {}).get("id", "")
        return False, str(d)[:200]
    except Exception as e:
        return False, f"HTTP {r.status_code} {e}"


async def main():
    await client.connect()
    st, sha = read_state()
    st.setdefault("bought", {})
    st.setdefault("contract_blacklist", {})

    triggered = []

    # 1) 先扫 debot_watcher_14_bot，更新黑名单
    try:
        watcher = await client.get_entity(WATCHER_BOT_USERNAME)
        async for msg in client.iter_messages(watcher, limit=SCAN_LIMIT):
            text = msg.message or ""
            if not text: continue
            for c in extract_addrs(text):
                st["contract_blacklist"][c] = True
        print(f"watcher scan done, blacklist size={len(st['contract_blacklist'])}")
    except Exception as e:
        print(f"watcher scan err: {e}")

    # 2) 扫 Empire 群组，找新合约
    try:
        empire = await client.get_entity(EMPIRE_GROUP)
        async for msg in client.iter_messages(empire, limit=SCAN_LIMIT):
            text = msg.message or ""
            if not text: continue
            # 只处理最近 30 分钟的消息
            if msg.date and (time.time() - msg.date.timestamp() > WINDOW_SEC):
                continue
            contracts = extract_addrs(text)
            for c in contracts:
                # 黑名单跳过
                if c in st["contract_blacklist"]:
                    continue
                # 已买过跳过
                if c in st["bought"]:
                    continue
                triggered.append((c, text, msg.date.timestamp()))
    except Exception as e:
        print(f"empire scan err: {e}")

    # 3) 去重触发列表（同一个合约只触发一次）
    seen = set()
    unique_triggered = []
    for c, text, ts in triggered:
        if c not in seen:
            seen.add(c)
            unique_triggered.append((c, text, ts))

    # 4) 执行触发：推送 + 买入
    for c, text, ts in unique_triggered:
        import html as H
        msg = (f"🚀 <b>Empire Smart Money 买入信号（云端）</b>\n"
               f"合约: <code>{c}</code>\n"
               f"买入: {BUY_AMOUNT} BNB | 止盈: +{STOP_EARN*100:.0f}% 全卖\n")
        try:
            await client.send_message(PUSH_CHAT_ID, msg, parse_mode="html", link_preview=False)
            print(f"pushed {c}")
        except Exception as e:
            print(f"push fail {c}: {e}")

        ok, info = dbotx_buy(c)
        if ok:
            st["bought"][c] = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "order_id": info,
                "amount": BUY_AMOUNT,
                "stop_earn": STOP_EARN,
                "source": "cloud",
            }
        print(f"dbotx {c}: ok={ok} {info}")

    # 5) 写回状态
    write_state(st, sha)
    print(f"done. triggered={len(unique_triggered)}, bought={len(st['bought'])}, blacklist={len(st['contract_blacklist'])}")
    await client.disconnect()


asyncio.run(main())
