#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dbotx 模拟器自动买入（云端版，GitHub Actions）
================================================
来源：仓库 seen.json（云端推送过的合约全集，monitor.py/speedrun_scan.py 写回）
动作：seen 中不在 dbotx_bought.json 的新地址 -> 模拟器 API 买入 1 SOL + 止盈 100%
去重：dbotx_bought.json（仓库内，买入后写回，workflow commit）
环境变量：DBOTX_API_KEY（GitHub Secrets）
说明：首次部署时 dbotx_bought.json 初始化为 seen.json 快照（历史推送不买，只买之后的）
"""
import json
import os
import pathlib
import time
import urllib.error
import urllib.request

SEEN_FILE = "seen.json"
BOUGHT_FILE = "dbotx_bought.json"
API_URL = "https://api-bot-v1.dbotx.com/simulator/sim_swap_order"

BUY_SOL = 1.0              # 每次买入 1 SOL
STOP_EARN = 1.0            # 止盈 100%（翻倍卖出全部，单次模式）
SLIPPAGE = 0.5             # 最大滑点


def load_json(f):
    p = pathlib.Path(f)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def load_seen():
    s = load_json(SEEN_FILE)
    if isinstance(s, list):
        return set(s)
    if isinstance(s, dict):
        return set(s)
    return set()


def api_buy(addr):
    key = os.environ.get("DBOTX_API_KEY", "")
    if not key:
        return False, "no DBOTX_API_KEY"
    body = json.dumps({
        "chain": "solana",
        "pair": addr,
        "walletId": "",
        "type": "buy",
        "amountOrPercent": BUY_SOL,
        "stopEarnPercent": STOP_EARN,
        "stopLossPercent": None,
        "priorityFee": "",
        "gasFeeDelta": 5,
        "maxFeePerGas": 100,
        "slippage": SLIPPAGE,
    }).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST")
    req.add_header("X-API-KEY", key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("err") is False:
            return True, data.get("res", {}).get("id", "")
        return False, str(data)
    except urllib.error.HTTPError as e:
        return False, "HTTP {} {}".format(e.code, e.read().decode("utf-8", "ignore")[:200])
    except Exception as e:
        return False, str(e)


def main():
    seen = load_seen()
    bought = load_json(BOUGHT_FILE)
    new = sorted(seen - set(bought))
    if not new:
        print("dbotx: no new push, seen={} bought={}".format(len(seen), len(bought)))
        return
    for addr in new:
        ok, info = api_buy(addr)
        bought[addr] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "order_id": info,
            "sol": BUY_SOL,
            "stop_earn": STOP_EARN,
            "ok": ok,
        }
        print("dbotx buy {} -> {} | {}".format("OK" if ok else "FAIL", addr, info))
    pathlib.Path(BOUGHT_FILE).write_text(
        json.dumps(bought, ensure_ascii=False, indent=1), encoding="utf-8")
    print("dbotx: done, bought={}".format(len(bought)))


if __name__ == "__main__":
    main()
