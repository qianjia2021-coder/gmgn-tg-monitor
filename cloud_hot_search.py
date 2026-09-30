#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GMGN 热搜第一监控 -> Telegram「GMGN热搜」群组（云端版，GitHub Actions）
=========================================================================
只推 BSC 链 1h 热搜第一（SOL 暂停，2026-09-29 用户指定）。双端共享状态文件 cloud_hot_state.json 逻辑不变。
双端共享状态文件 cloud_hot_state.json（存于仓库），本地/云端双向同步去重；
推送前再扫群内最近 30 条消息兜底，防止本地/云端竞态重复。

Env:
  TG_API_ID / TG_API_HASH / TG_STRING_SESSION   Telegram 用户会话
  GMGN_API_KEY                                    gmgn-cli 配置
  GITHUB_TOKEN                                    Actions 自动提供，用于读写状态文件
  TG_HOTSEARCH_CHAT_ID                           目标群（默认 5499948080 = GMGN热搜）
"""
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import InputPeerChat

REPO = "qianjia2021-coder/gmgn-tg-monitor"
STATE_PATH = "cloud_hot_state.json"
BOUGHT_PATH = "cloud_dbotx_hot_bought.json"
GH_API = "https://api.github.com"
CHAINS = ["sol"]          # (09-30) 只监控 SOL，BSC 暂停
HOT_INTERVAL = "1h"
LIMIT = 20
GOLDEN_GROUP = "chengzi_golden"          # 前置条件：合约须在该群发送过才推送
GOLDEN_SCAN_LIMIT = 200                  # 扫该群最近 200 条消息
TARGET_CHAT = int(os.environ.get("TG_HOTSEARCH_CHAT_ID", "5499948080"))
TARGET_NAME = "GMGN热搜"
# ④ 黑名单钱包过滤（用户 09-30 指定）：该钱包买入过的合约一律不推不买
BLACKLIST_WALLETS = [
    "suqh5sHtr8HyJ7q8scBimULPkPpA557prMG47xCHQfK",
]
ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b|\b0x[a-fA-F0-9]{40}\b")

# dbotx 模拟器自动买入（与本地一致）：SOL 0.1 / BSC 0.1，止盈 +50% 全卖
DBOTX_SWAP_URL = "https://api-bot-v1.dbotx.com/simulator/sim_swap_order"
DBOTX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
DBOTX_CHAIN_AMOUNT = {
    "sol": {"chain": "solana", "amount": 0.1},
    "bsc": {"chain": "bsc", "amount": 0.1},
}
STOP_EARN = 0.5
SLIPPAGE = 0.5
MAX_BUY_RETRY = 5


def gh_headers():
    return {
        "Authorization": "token " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
        "User-Agent": "gmgn-hotsearch-monitor",
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
    body = {
        "message": msg,
        "content": base64.b64encode(payload).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{REPO}/contents/{path}",
                     headers=gh_headers(), json=body, timeout=30)
    r.raise_for_status()


def read_state():
    return read_json_file(STATE_PATH)


def write_state(state, sha):
    write_json_file(STATE_PATH, state, sha, "sync hot state from cloud [skip ci]")


def read_bought():
    return read_json_file(BOUGHT_PATH)


def write_bought(bought, sha):
    write_json_file(BOUGHT_PATH, bought, sha, "sync dbotx hot bought from cloud [skip ci]")


def api_buy(chain, addr):
    """dbotx 模拟器买入（SOL 0.1 / BSC 0.1，止盈 +50% 全卖）。返回 (ok, id_or_err)"""
    key = os.environ.get("DBOTX_API_KEY", "")
    if not key:
        return False, "no DBOTX_API_KEY"
    cfg = DBOTX_CHAIN_AMOUNT.get(chain)
    if not cfg:
        return False, "chain unsupported: {}".format(chain)
    body = {
        "chain": cfg["chain"], "pair": addr, "walletId": "", "type": "buy",
        "amountOrPercent": cfg["amount"], "stopEarnPercent": STOP_EARN,
        "stopLossPercent": None, "priorityFee": "", "gasFeeDelta": 5,
        "maxFeePerGas": 100, "slippage": SLIPPAGE,
    }
    r = requests.post(DBOTX_SWAP_URL, json=body,
                      headers={"X-API-KEY": key,
                               "Content-Type": "application/json",
                               "accept": "application/json",
                               "User-Agent": DBOTX_UA,
                               "Origin": "https://dbotx.com",
                               "Referer": "https://dbotx.com/"}, timeout=30)
    try:
        data = r.json()
        if data.get("err") is False:
            return True, data.get("res", {}).get("id", "")
        return False, str(data)[:300]
    except Exception as e:
        return False, "HTTP {} {}".format(r.status_code, e)


def retry_failed_buys(bought):
    changed = False
    for addr, rec in list(bought.items()):
        if rec.get("ok"):
            continue
        if int(rec.get("attempts") or 0) >= MAX_BUY_RETRY:
            continue
        ok, info = api_buy(rec.get("chain") or "", addr)
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        if ok:
            rec["ok"] = True
            rec["order_id"] = info
            rec.pop("err", None)
            print("dbotx retry OK: {}".format(addr))
        else:
            print("dbotx retry fail({}/{}): {} | {}".format(
                rec["attempts"], MAX_BUY_RETRY, addr, info))
        changed = True
    return bought, changed


def run_gmgn(args):
    for attempt in (1, 2):
        proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
        if proc.returncode == 0:
            return proc.stdout
        err = (proc.stderr or proc.stdout or "")
        if "429" in err and attempt == 1:
            print("429 rate limited, waiting 35s and retrying...")
            time.sleep(35)
            continue
        raise RuntimeError("gmgn-cli failed rc={}: {}".format(proc.returncode, err[-500:]))


def fetch_hot(chain):
    args = ["gmgn-cli", "market", "hot-searches", "--chain", chain,
            "--interval", HOT_INTERVAL, "--limit", str(LIMIT), "--raw"]
    print("cmd: " + " ".join(args))
    out = run_gmgn(args)
    data = json.loads(out)
    if isinstance(data, list) and data:
        return data[0].get("tokens") or []
    return (data.get("data") or {}).get("rank") or []


def wallet_bought_token(chain, addr, wallet):
    """黑名单检查：该钱包是否在合约 top-100 交易者中买入过（weight=5）。检查失败保守拦截。"""
    args = ["gmgn-cli", "token", "traders", "--chain", chain, "--address", addr,
            "--limit", "100", "--raw"]
    try:
        out = run_gmgn(args, timeout=60)
        data = json.loads(out)
    except Exception as e:
        print("黑名单检查失败 {}: {}".format(addr, e))
        return True
    for w in data.get("list") or []:
        if (w.get("address") or "") == wallet and float(w.get("buy_volume_cur") or 0) > 0:
            return True
    return False


def get_rank1(tokens):
    for t in tokens:
        if int(t.get("rank") or 0) == 1:
            return t
    return tokens[0] if tokens else None


def _fmt_price(v):
    """价格格式化：大额保留小数位，小额保留足够精度去尾零"""
    try:
        p = float(v or 0)
    except (TypeError, ValueError):
        return "0"
    if p == 0:
        return "0"
    if p >= 1000:
        return "{:,.2f}".format(p)
    if p >= 1:
        return "{:,.4f}".format(p)
    s = "{:.12f}".format(p).rstrip("0").rstrip(".")
    return s


def fmt_msg(t, chain):
    sym = t.get("symbol") or ""
    name = t.get("name") or ""
    addr = t.get("address") or ""
    heat = t.get("visiting_count") or 0
    chg1h = round(float(t.get("price_change_percent1h") or 0), 1)
    liq = round(float(t.get("liquidity") or 0))
    lp = t.get("launchpad_platform") or ""
    holders = int(t.get("holder_count") or 0)
    swaps = int(t.get("swaps") or 0)
    return "\n".join([
        "🔥 <b>GMGN 热搜榜</b>  #{} {}".format(sym, name),
        "链: {} | 平台: {} | 热搜热度: {}".format(chain.upper(), lp or "-", heat),
        "1h涨跌: {}% | 价格 ${} | 流动性 ${:,}".format(chg1h, _fmt_price(t.get("price")), liq),
        "持有人: {:,} | swaps: {:,}".format(holders, swaps),
        "GMGN: https://gmgn.ai/{}/token/{}".format(chain, addr),
        "<code>{}</code>".format(addr),
    ])


async def main():
    state, sha = read_state()
    bought, bought_sha = read_bought()
    print("state entries: {} chains | bought: {}".format(len(state), len(bought)))
    # 1) 重试历史买入失败的合约
    try:
        bought, _ = retry_failed_buys(bought)
    except Exception as e:
        print("buy retry error: {}".format(e))
    client = TelegramClient(StringSession(os.environ["TG_STRING_SESSION"]),
                            int(os.environ["TG_API_ID"]),
                            os.environ["TG_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        print("FATAL: not authorized")
        sys.exit(1)
    peer = InputPeerChat(TARGET_CHAT)
    # 兜底去重：群内最近 30 条消息里已出现的合约
    already = set()
    async for msg in client.iter_messages(peer, limit=30):
        if msg.message:
            for m in ADDR_RE.findall(msg.message):
                already.add(m.lower())
    print("already in group (last 30): {}".format(len(already)))
    # (09-30 临时) 已去掉 chengzi_golden 前置限制：热搜前2都推
    pushed = 0
    state_changed = False
    bought_changed = False
    for chain in CHAINS:
        try:
            tokens = fetch_hot(chain)
        except Exception as e:
            print("[{}] fetch failed: {}".format(chain, e))
            continue
        top2 = tokens[:2]
        if not top2:
            print("[{}] empty rank".format(chain))
            continue
        for rank, tok in enumerate(top2, 1):
            addr = (tok.get("address") or "").strip()
            sym = tok.get("symbol") or ""
            print("[{}] hot #{}: {} | {}".format(chain, rank, sym, addr))
            rec = state.get(chain) or {"last": "", "seen": []}
            seen = set(rec.get("seen") or [])
            if addr in seen or addr.lower() in already:
                print("[{}] already pushed, skip".format(chain))
                rec["last"] = addr
                state[chain] = rec
                continue
            # ④ 黑名单钱包过滤：该钱包买入过的合约不推不买
            if BLACKLIST_WALLETS:
                blocked = False
                for w in BLACKLIST_WALLETS:
                    try:
                        if wallet_bought_token(chain, addr, w):
                            blocked = True
                            break
                    except Exception as e:
                        print("blacklist check error: {}".format(e))
                        blocked = True
                        break
                if blocked:
                    print("[{}] 黑名单拦截（不推不买）: {} | {}".format(chain, sym, addr))
                    seen.add(addr)
                    rec["last"] = addr
                    rec["seen"] = sorted(seen)
                    state[chain] = rec
                    state_changed = True
                    continue

            try:
                await client.send_message(peer, fmt_msg(tok, chain),
                                          parse_mode="html", link_preview=False)
                print("[{}] PUSHED hot #{}: {} | {}".format(chain, rank, sym, addr))
            except Exception as e:
                print("[{}] push failed: {}".format(chain, e))
                continue
            seen.add(addr)
            rec["last"] = addr
            rec["seen"] = sorted(seen)
            state[chain] = rec
            pushed += 1
            # 2) 推送成功后：dbotx 模拟器自动买入（SOL 0.1 / BSC 0.1，止盈 +50% 全卖）
            if addr not in bought:
                ok, info = api_buy(chain, addr)
                bought[addr] = {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "chain": chain,
                    "amount": DBOTX_CHAIN_AMOUNT[chain]["amount"],
                    "stop_earn": STOP_EARN,
                    "order_id": info if ok else "",
                    "ok": ok,
                    "attempts": 1,
                }
                if not ok:
                    bought[addr]["err"] = str(info)[:200]
                print("[{}] dbotx buy {}: {} | {}".format(
                    chain, "OK" if ok else "FAIL", sym, info))
                bought_changed = True
            await asyncio.sleep(1.5)
        state_changed = True
    await client.disconnect()
    if state_changed:
        try:
            write_state(state, sha)
            print("state written")
        except Exception as e:
            print("state write failed: {}".format(e))
    if bought_changed:
        try:
            write_bought(bought, bought_sha)
            print("bought written")
        except Exception as e:
            print("bought write failed: {}".format(e))
    print("done pushed={} bought={}".format(pushed, len(bought)))


if __name__ == "__main__":
    asyncio.run(main())
