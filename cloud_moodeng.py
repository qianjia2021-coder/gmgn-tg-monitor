#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MooDengPresidentCallers 群合约监控 - 云端版（GitHub Actions）
================================================================
本机关机时由云端每 5 分钟接管：拉群新消息，KOL 过滤后 dbotx 模拟买 1 SOL，
止盈 +50% 全卖。状态存仓库 cloud_moodeng_state.json / cloud_moodeng_bought.json。

注意：配置（BUY_SOL/STOP_EARN/KOL_MIN/TARGET_USERNAME 等）与本地
moodeng_auto_buy.py 顶部 CONFIG 段保持一致，改设置两边同步改。

Env:
  MOODENG_TG_API_ID / MOODENG_TG_API_HASH / MOODENG_TG_STRING_SESSION
  DBOTX_API_KEY
  GITHUB_TOKEN   Actions 自动提供
"""
import asyncio, base64, json, os, re, subprocess, sys, time
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

REPO = "qianjia2021-coder/gmgn-tg-monitor"
STATE_PATH = "cloud_moodeng_state.json"
BOUGHT_PATH = "cloud_moodeng_bought.json"
GH_API = "https://api.github.com"

# === CONFIG（与本地 moodeng_auto_buy.py 保持一致） ===
TARGETS = ["MooDengPresidentCallers", "beijngdontlie", "logandegen", "SolanaWhalesMarket", "lycagamble"]
KOL_MIN = 2
BUY_SOL = 1.0
STOP_EARN = 0.5
SLIPPAGE = 0.5
# =====================================================

DBOTX_SWAP_URL = "https://api-bot-v1.dbotx.com/simulator/sim_swap_order"
DBOTX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

SOL_NATIVE = "So11111111111111111111111111111111111111112"
ADDR_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def gh_headers():
    return {"Authorization": "token " + os.environ["GITHUB_TOKEN"],
            "Accept": "application/vnd.github+json",
            "User-Agent": "moodeng-monitor"}


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


def extract_addrs(text):
    out = []
    for c in ADDR_RE.findall(text or ""):
        if c != SOL_NATIVE:
            out.append(c)
    return out


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
    try:
        out = run_gmgn(["gmgn-cli", "token", "info", "--chain", "sol", "--address", addr], timeout=45)
        data = json.loads(out)
    except Exception as e:
        print("info fail {}: {}".format(addr, e))
        return 0, False
    wts = data.get("wallet_tags_stat") or {}
    return int(wts.get("renowned_wallets") or 0), True


def rug_ratio(addr):
    target = addr.lower()
    try:
        out = run_gmgn(["gmgn-cli", "market", "trending", "--chain", "sol",
                        "--interval", "24h", "--limit", "100", "--raw"], timeout=45)
        d = json.loads(out)
        for r in (d.get("data") or {}).get("rank") or []:
            if (r.get("address") or "").lower() == target:
                v = r.get("rug_ratio")
                return (None if v is None else float(v)), True
    except Exception:
        pass
    try:
        out = run_gmgn(["gmgn-cli", "market", "trenches", "--chain", "sol", "--raw"], timeout=60)
        d = json.loads(out.stdout) if hasattr(out, 'stdout') else json.loads(out)
        for rows in d.values():
            if not isinstance(rows, list):
                continue
            for r in rows:
                if (r.get("address") or "").lower() == target:
                    v = r.get("rug_ratio")
                    return (None if v is None else float(v)), True
    except Exception:
        pass
    return None, True


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
    baseline = state.get("baseline") or []
    last_id = int(state.get("last_id") or 0)
    baseline_set = set(baseline)
    print("last_id={} seen={} bought={} baseline={}".format(last_id, len(seen), len(bought), len(baseline_set)))

    client = TelegramClient(StringSession(os.environ["MOODENG_TG_STRING_SESSION"]),
                            int(os.environ["MOODENG_TG_API_ID"]),
                            os.environ["MOODENG_TG_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        print("FATAL: not authorized"); sys.exit(1)

    changed = False
    for target in TARGETS:
        try:
            peer = await client.get_entity(target)
        except Exception as e:
            print("群 @{} 访问失败: {}".format(target, e))
            continue
        print("--- 扫描 @{} ---".format(target))
        async for m in client.iter_messages(peer, limit=200):
            if m.reply_to_msg_id:
                continue
            text = m.message or ""
            addrs = extract_addrs(text)
            for addr in addrs:
                if addr in seen or addr in bought or addr in baseline_set:
                    continue
                print("new [{}] #{}: {}".format(target, m.id, addr))
                kol, ok = kol_count(addr)
                if not ok:
                    print("  KOL 查询失败，本轮跳过")
                    continue
                if kol < KOL_MIN:
                    seen[addr] = time.strftime("%Y-%m-%d %H:%M:%S")
                    print("  KOL={}<{} 跳过".format(kol, KOL_MIN))
                    changed = True
                    continue
                rr, rok = rug_ratio(addr)
                if not rok:
                    print("  rug 查询异常，跳过")
                    continue
                if rr is not None and rr >= 1.0:
                    seen[addr] = time.strftime("%Y-%m-%d %H:%M:%S")
                    print("  rug={:.0%} 跳过".format(rr))
                    changed = True
                    continue
                bok, info = api_buy(addr)
                bought[addr] = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "order_id": info if bok else "", "sol": BUY_SOL,
                                "stop_earn": STOP_EARN, "kol": kol, "rug_ratio": rr,
                                "ok": bok}
                seen[addr] = time.strftime("%Y-%m-%d %H:%M:%S")
                print("  KOL={} rug={} buy {}: {}".format(kol, rr, "OK" if bok else "FAIL", info))
                changed = True
                await asyncio.sleep(2)
    await client.disconnect()

    state["last_id"] = last_id
    state["seen"] = seen
    state["baseline"] = baseline
    try:
        write_json_file(STATE_PATH, state, sha, "sync moodeng state from cloud [skip ci]")
        write_json_file(BOUGHT_PATH, bought, bsha, "sync moodeng bought from cloud [skip ci]")
        print("state written, last_id={}".format(last_id))
    except Exception as e:
        print("write state failed: {}".format(e))
    print("done changed={}".format(changed))


if __name__ == "__main__":
    asyncio.run(main())
