#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OnChain Alpha Trench 合约中继 - 云端版（GitHub Actions）
================================================================
本地关机时由 GitHub Actions 每 5 分钟接管：
  1. 读 heartbeat.txt：若本地 4 分钟内有心跳 -> 本地在线，直接退出
  2. 扫源群 @onchainalphatrench 最近 60 条消息，提取合约地址
  3. 读云端 cloud_alpha_seen.json + 扫目标群最近 20 条消息（双兜底去重）
  4. 新合约 -> 推到目标群(chat_id=5194908956) + dbotx 自动买入
  5. 写回 cloud_alpha_seen.json + cloud_alpha_bought.json

Secrets:
  TG_STRING_SESSION / TG_API_ID / TG_API_HASH
  DBOTX_API_KEY   dbotx 模拟器 API key
  GITHUB_TOKEN    Actions 自动提供
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
BOUGHT_PATH = "cloud_alpha_bought.json"
HEARTBEAT_PATH = "alpha_heartbeat.txt"
GH_API = "https://api.github.com"

SOURCE_GROUP = "onchainalphatrench"
TARGET_PEER = InputPeerChat(5194908956)
SCAN_LIMIT = 60
TARGET_DEDUP_LIMIT = 20                   # 推送前扫目标群最近 N 条兜底去重
HEARTBEAT_FRESH_SECONDS = 4 * 60

# dbotx 配置（与本地一致）
DBOTX_SWAP_URL = "https://api-bot-v1.dbotx.com/simulator/sim_swap_order"
DBOTX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
DBOTX_BUY_AMOUNT = 1.0
DBOTX_STOP_EARN = 0.99
DBOTX_SLIPPAGE = 0.5

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
    try:
        text, _ = read_text_file(HEARTBEAT_PATH)
        text = text.strip()
        if not text:
            return False, "no heartbeat"
        ts = time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
        age = time.time() - ts
        if age < HEARTBEAT_FRESH_SECONDS:
            return True, f"heartbeat age {int(age)}s"
        return False, f"heartbeat stale age {int(age)}s"
    except Exception as e:
        return False, f"heartbeat check err: {e}"


def extract_addrs(text):
    """优先提取独立成行的地址，避免从描述文字误提取。"""
    if not text:
        return set()
    out = set()
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = ADDR_RE.fullmatch(s) or ADDR_RE.search(s)
        if m:
            a = m.group(0).lower()
            if _valid(a):
                out.add(a)
    if not out:
        for m in ADDR_RE.findall(text):
            a = m.lower()
            if _valid(a):
                out.add(a)
    return out


def _valid(a):
    if a.startswith("0x"):
        return len(a) == 42
    return 32 <= len(a) <= 44


def _chain_of(addr):
    return "bsc" if addr.startswith("0x") else "sol"


def detect_chain(addr, msg_text=""):
    """根据消息文本判断链（优先级高于地址格式）"""
    t = (msg_text or "").upper()
    if "ROBINHOOD" in t or "ROB" in t.split() or "RH" in t.split():
        return "robinhood"
    if "ETH" in t.split() or "ETHEREUM" in t:
        return "eth"
    if "BSC" in t or "BNB" in t.split():
        return "bsc"
    if "SOL" in t or "SOLANA" in t:
        return "sol"
    return "bsc" if addr.startswith("0x") else "sol"


def api_buy(addr, chain="bsc"):
    """dbotx 模拟器买入。chain: bsc / solana / robinhood"""
    dbotx_chain = "solana" if chain == "sol" else chain
    body = json.dumps({
        "chain": dbotx_chain,
        "pair": addr,
        "walletId": "",
        "type": "buy",
        "amountOrPercent": DBOTX_BUY_AMOUNT,
        "stopEarnPercent": DBOTX_STOP_EARN,
        "stopLossPercent": None,
        "priorityFee": "",
        "gasFeeDelta": 5,
        "maxFeePerGas": 100,
        "slippage": DBOTX_SLIPPAGE,
    }).encode("utf-8")
    req = requests.Request("POST", DBOTX_SWAP_URL, data=body,
                           headers={
                               "X-API-KEY": os.environ.get("DBOTX_API_KEY", ""),
                               "Content-Type": "application/json",
                               "accept": "application/json",
                               "User-Agent": DBOTX_UA,
                               "Origin": "https://dbotx.com",
                               "Referer": "https://dbotx.com/",
                           })
    try:
        with requests.Session() as s:
            r = s.send(req.prepare(), timeout=30)
        data = r.json()
        if data.get("err") is False:
            return True, data.get("res", {}).get("id", "")
        return False, str(data)[:200]
    except Exception as e:
        return False, str(e)[:200]


async def run():
    online, reason = local_online()
    log(f"local online check: {online} ({reason})")
    if online:
        log("本地在线，云端不接管，退出")
        return 0

    # 读云端 seen + bought
    seen_data, seen_sha = read_json_file(SEEN_PATH)
    if not isinstance(seen_data, dict):
        seen_data = {}
    seen = set(a.lower() for a in seen_data.get("seen", []))
    bought_data, bought_sha = read_json_file(BOUGHT_PATH)
    if not isinstance(bought_data, dict):
        bought_data = {}
    log(f"云端 seen={len(seen)}, bought={len(bought_data)}")

    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    string_session = os.environ["TG_STRING_SESSION"]
    client = TelegramClient(StringSession(string_session), api_id, api_hash)
    pushed = 0
    bought_new = 0
    try:
        await client.connect()
        if not await client.is_user_authorized():
            log("StringSession 未授权")
            return 1
        src_peer = await client.get_entity(SOURCE_GROUP)
        log(f"源群解析: {getattr(src_peer,'title',SOURCE_GROUP)}")

        # 兜底去重：扫目标群最近 N 条消息，已出现的合约不重复推
        target_addrs = set()
        async for msg in client.iter_messages(TARGET_PEER, limit=TARGET_DEDUP_LIMIT):
            if msg.message:
                target_addrs |= extract_addrs(msg.message)
        log(f"目标群最近 {TARGET_DEDUP_LIMIT} 条含 {len(target_addrs)} 个合约（兜底去重）")

        async for msg in client.iter_messages(src_peer, limit=SCAN_LIMIT):
            text = msg.message or ""
            if not text:
                continue
            addrs = extract_addrs(text)
            if not addrs:
                continue
            # 只保留 BSC（0x 开头）；SOL(base58)/ETH/Robinhood 一律忽略
            bsc_addrs = {a for a in addrs if a.startswith("0x")}
            if not bsc_addrs:
                continue
            new_addrs = bsc_addrs - seen
            if not new_addrs:
                continue
            # 兜底：目标群最近已出现过的也跳过
            real_new = new_addrs - target_addrs
            if not real_new:
                seen |= bsc_addrs
                continue

            # 转发格式：第一行标题 + code 地址（每个 BSC 合约单独一条消息）
            first_line = ""
            for line in text.splitlines():
                line = line.strip()
                if line:
                    first_line = line
                    break
            header = ADDR_RE.sub("", first_line).strip()

            for a in sorted(real_new):
                lines = ["📡 <b>BSC 新合约（云端补推）</b>", ""]
                if header:
                    lines.append(header)
                    lines.append("")
                lines.append(f"<code>{a}</code>")
                out = "\n".join(lines)
                try:
                    await client.send_message(TARGET_PEER, out,
                                             parse_mode="html",
                                             link_preview=False)
                    log(f"已单独转发 BSC 合约: {a}")
                    pushed += 1
                    target_addrs.add(a)
                except Exception as e:
                    log(f"发送单条 BSC 合约失败（跳过该地址）: {a} | {e}")
                    continue
                seen.add(a)

                # dbotx 自动买入（只对真正新的、且没买过的）
                if a in bought_data and bought_data[a].get("ok"):
                    continue
                chain_a = detect_chain(a, text)
                if chain_a in ("eth", "robinhood"):
                    log(f"{chain_a} 链，跳过 dbotx 买入: {a}")
                    bought_data[a] = {
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "chain": chain_a, "ok": True,
                        "skipped": f"{chain_a}_no_buy"}
                    continue
                ok, info = api_buy(a, chain_a)
                bought_data[a] = {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "chain": chain_a,
                    "amount": DBOTX_BUY_AMOUNT,
                    "stop_earn": DBOTX_STOP_EARN,
                    "ok": ok,
                }
                if ok:
                    bought_data[a]["order_id"] = info
                    bought_new += 1
                    log(f"dbotx 买入成功: {a} 链={chain_a} 订单={info}")
                else:
                    bought_data[a]["err"] = str(info)[:200]
                    log(f"dbotx 买入失败: {a} | {info}")
    finally:
        await client.disconnect()

    # 写回 seen + bought
    seen_data["seen"] = sorted(seen)
    seen_data["last_cloud_push"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_json_file(SEEN_PATH, seen_data, seen_sha,
                    f"alpha cloud: pushed {pushed} msgs, seen {len(seen)} [skip ci]")
    write_json_file(BOUGHT_PATH, bought_data, bought_sha,
                    f"alpha cloud: bought {bought_new} new, total {len(bought_data)} [skip ci]")
    log(f"本轮完成: pushed={pushed}, bought_new={bought_new}, seen={len(seen)}")
    return 0


if __name__ == "__main__":
    sys_rc = asyncio.run(run())
    raise SystemExit(sys_rc or 0)
