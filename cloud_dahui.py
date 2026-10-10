#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web3_gxfc「大厅」(topic 23) 合约监控 - 云端版（GitHub Actions）
=================================================================
本机关机时由云端接管：拉大厅新消息，KOL 过滤后 dbotx 模拟买 1 SOL，
止盈 +50% 全卖。状态存仓库 cloud_dahui_state.json / cloud_dahui_bought.json。

Env:
  DAHUI_TG_API_ID / DAHUI_TG_API_HASH / DAHUI_TG_STRING_SESSION   专属会话
  GMGN_API_KEY        gmgn-cli
  DBOTX_API_KEY       dbotx 模拟器
  GITHUB_TOKEN        Actions 自动提供
"""
import asyncio, base64, json, os, re, subprocess, sys, time
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

REPO = "qianjia2021-coder/gmgn-tg-monitor"
STATE_PATH = "cloud_dahui_state.json"
BOUGHT_PATH = "cloud_dahui_bought.json"
GH_API = "https://api.github.com"

CHANNEL = "web3_gxfc"
TOPIC_ID = 23
KOL_MIN = 1
BUY_SOL = 1.0
STOP_EARN = 0.5
SLIPPAGE = 0.5

DBOTX_SWAP_URL = "https://api-bot-v1.dbotx.com/simulator/sim_swap_order"
DBOTX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

SOL_NATIVE = "So11111111111111111111111111111111111111112"
ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def gh_headers():
    return {"Authorization": "token " + os.environ["GITHUB_TOKEN"],
            "Accept": "application/vnd.github+json",
            "User-Agent": "dahui-monitor"}


def read_json_file(path):
    r = requests.get(f"{GH_API}/repos/{REPO}/contents/{path}", headers=gh_headers(), timeout=20)
    if r.status_code == 404:
        return {}, None
    r.raise_for_status()
    j = r.json()
    return json.loads(base64.b64decode(j["content"]).decode()), j["sha"]


def write_json_file(path, data, sha, msg):
    payload = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    body = {"message": msg, "content": base64.b64encode(payload).decode(), "branch": "main"}
    if sha:
        body["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{REPO}/contents/{path}", headers=gh_headers(), json=body, timeout=30)
    r.raise_for_status()


def extract_addr(text):
    if not text:
        return None
    for c in reversed(ADDR_RE.findall(text)):
        if c != SOL_NATIVE:
            return c
    return None


def parse_line(text, key):
    m = re.search(key + r"[:：]\s*([^\n|]+)", text or "")
    return m.group(1).strip() if m else ""


def run_gmgn(args, timeout=60):
    for attempt in (1, 2):
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if p.returncode == 0:
            return p.stdout
        err = (p.stderr or p.stdout or "")
        if "429" in err and attempt == 1:
            print("429, wait 35s retry"); time.sleep(35); continue
        raise RuntimeError("gmgn-cli rc={}: {}".format(p.returncode, err[-300:]))


def kol_count(addr):
    """KOL 数 = wallet_tags_stat.renowned_wallets（知名/KOL 钱包）。"""
    try:
        out = run_gmgn(["gmgn-cli", "token", "info", "--chain", "sol",
                        "--address", addr], timeout=45)
        data = json.loads(out)
    except Exception as e:
        print("info fail {}: {}".format(addr, e))
        return 0, False
    wts = data.get("wallet_tags_stat") or {}
    return int(wts.get("renowned_wallets") or 0), True


def rug_ratio(addr):
    """查 GMGN 跑路概率（0-1）。trending+trenches 双查。
    返回 (value, found)：found=False 表示 GMGN 无数据，应跳过。"""
    target = addr.lower()
    # trending
    try:
        out = run_gmgn(["gmgn-cli", "market", "trending", "--chain", "sol",
                        "--interval", "24h", "--limit", "100", "--raw"], timeout=45)
        d = json.loads(out)
        for r in (d.get("data") or {}).get("rank") or []:
            if (r.get("address") or "").lower() == target:
                v = r.get("rug_ratio")
                return (None if v is None else float(v)), v is not None
    except Exception:
        pass
    # trenches
    try:
        out = run_gmgn(["gmgn-cli", "market", "trenches", "--chain", "sol", "--raw"], timeout=60)
        d = json.loads(out)
        for rows in d.values():
            if not isinstance(rows, list):
                continue
            for r in rows:
                if (r.get("address") or "").lower() == target:
                    v = r.get("rug_ratio")
                    return (None if v is None else float(v)), v is not None
    except Exception:
        pass
    return None, False


def api_buy(addr):
    key = os.environ.get("DBOTX_API_KEY", "")
    if not key:
        return False, "no DBOTX_API_KEY"
    body = {"chain": "solana", "pair": addr, "walletId": "", "type": "buy",
            "amountOrPercent": BUY_SOL, "stopEarnPercent": STOP_EARN,
            "stopLossPercent": None, "priorityFee": "", "gasFeeDelta": 5,
            "maxFeePerGas": 100, "slippage": SLIPPAGE}
    headers = {"X-API-KEY": key, "Content-Type": "application/json",
               "accept": "application/json", "User-Agent": DBOTX_UA,
               "Origin": "https://dbotx.com", "Referer": "https://dbotx.com/"}
    for attempt in range(3):
        try:
            r = requests.post(DBOTX_SWAP_URL, json=body, headers=headers, timeout=30)
            d = r.json()
            if d.get("err") is False:
                return True, d.get("res", {}).get("id", "")
            if r.status_code >= 500 and attempt < 2:
                time.sleep(2); continue
            return False, str(d)[:300]
        except Exception as e:
            if attempt < 2:
                time.sleep(2); continue
            return False, "HTTP {}".format(e)


async def main():
    state, sha = read_json_file(STATE_PATH)
    bought, bsha = read_json_file(BOUGHT_PATH)
    seen = state.get("seen") or {}
    last_id = int(state.get("last_id") or 0)
    print("last_id={} seen={} bought={}".format(last_id, len(seen), len(bought)))

    client = TelegramClient(StringSession(os.environ["DAHUI_TG_STRING_SESSION"]),
                            int(os.environ["DAHUI_TG_API_ID"]),
                            os.environ["DAHUI_TG_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        print("FATAL: not authorized"); sys.exit(1)
    peer = await client.get_entity(CHANNEL)

    changed = False
    async for m in client.iter_messages(peer, reply_to=TOPIC_ID, min_id=last_id):
        if m.id > last_id:
            last_id = m.id
        addr = extract_addr(m.message or "")
        if not addr or addr in seen or addr in bought:
            continue
        name = parse_line(m.message or "", "名称")
        mcap = parse_line(m.message or "", "市值")
        print("new #{}: {} | {} | {}".format(m.id, name, mcap, addr))
        kol, ok = kol_count(addr)
        if not ok:
            print("  info 查询失败，本轮跳过下轮再查")
            continue
        if kol < KOL_MIN:
            seen[addr] = time.strftime("%Y-%m-%d %H:%M:%S")
            print("  KOL={}<{} 跳过".format(kol, KOL_MIN))
            changed = True
            continue
        rr, found = rug_ratio(addr)
        if found and rr >= 1.0:
            seen[addr] = time.strftime("%Y-%m-%d %H:%M:%S")
            print("  跑路概率={:.0%}，跳过".format(rr))
            changed = True
            continue
        bok, info = api_buy(addr)
        bought[addr] = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "order_id": info if bok else "", "sol": BUY_SOL,
                        "stop_earn": STOP_EARN, "symbol": name, "kol": kol,
                        "rug_ratio": rr, "ok": bok, "err": "" if bok else str(info)[:200]}
        seen[addr] = time.strftime("%Y-%m-%d %H:%M:%S")
        print("  KOL={} rug={:.0%} buy {}: {}".format(kol, rr, "OK" if bok else "FAIL", info))
        changed = True
        await asyncio.sleep(2)
    await client.disconnect()

    state["last_id"] = last_id
    state["seen"] = seen
    try:
        write_json_file(STATE_PATH, state, sha, "sync dahui state from cloud [skip ci]")
        write_json_file(BOUGHT_PATH, bought, bsha, "sync dahui bought from cloud [skip ci]")
        print("state written, last_id={}".format(last_id))
    except Exception as e:
        print("write state failed: {}".format(e))
    print("done changed={}".format(changed))


if __name__ == "__main__":
    asyncio.run(main())
