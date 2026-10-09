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
    """返回 (kol_bought, ok)"""
    try:
        out = run_gmgn(["gmgn-cli", "token", "traders", "--chain", "sol",
                        "--address", addr, "--limit", "100", "--raw"])
        data = json.loads(out)
    except Exception as e:
        print("traders fail {}: {}".format(addr, e))
        return 0, False
    lst = data.get("list") or []
    kol = sum(1 for w in lst
              if ((w.get("avatar") or "").strip() or (w.get("name") or "").strip()
                   or (w.get("twitter_username") or "").strip())
              and float(w.get("buy_volume_cur") or 0) > 0)
    return kol, True


def api_buy(addr):
    key = os.environ.get("DBOTX_API_KEY", "")
    if not key:
        return False, "no DBOTX_API_KEY"
    body = {"chain": "solana", "pair": addr, "walletId": "", "type": "buy",
            "amountOrPercent": BUY_SOL, "stopEarnPercent": STOP_EARN,
            "stopLossPercent": None, "priorityFee": "", "gasFeeDelta": 5,
            "maxFeePerGas": 100, "slippage": SLIPPAGE}
    r = requests.post(DBOTX_SWAP_URL, json=body,
                      headers={"X-API-KEY": key, "Content-Type": "application/json",
                               "accept": "application/json", "User-Agent": DBOTX_UA,
                               "Origin": "https://dbotx.com", "Referer": "https://dbotx.com/"},
                      timeout=30)
    try:
        d = r.json()
        if d.get("err") is False:
            return True, d.get("res", {}).get("id", "")
        return False, str(d)[:300]
    except Exception as e:
        return False, "HTTP {} {}".format(r.status_code, e)


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
            print("  traders 查询失败，本轮跳过下轮再查")
            continue
        if kol < KOL_MIN:
            seen[addr] = time.strftime("%Y-%m-%d %H:%M:%S")
            print("  KOL={}<{} 跳过".format(kol, KOL_MIN))
            changed = True
            continue
        bok, info = api_buy(addr)
        bought[addr] = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "order_id": info if bok else "", "sol": BUY_SOL,
                        "stop_earn": STOP_EARN, "symbol": name, "kol": kol,
                        "ok": bok, "err": "" if bok else str(info)[:200]}
        seen[addr] = time.strftime("%Y-%m-%d %H:%M:%S")
        print("  KOL={} buy {}: {}".format(kol, "OK" if bok else "FAIL", info))
        changed = True
        await asyncio.sleep(1.5)
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
