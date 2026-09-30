#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GMGN 放量信号监控 -> Telegram「GMGN热搜」群组（云端版，GitHub Actions）
=========================================================================
规则与本地 vol_spike_monitor.py 完全一致（用户 09-30 指定）：
  ① 粗筛（榜单字段，任一命中）：hot_level>=2 或 swaps>=1000 或 1h涨幅>=100%
  ② 质量闸门（全部满足）：bot<=55% · 流动性>=$50K · smart>=3 · rug<=0.3
                           · 非 CTO 仿盘 · dev 已清仓(creator_close)
  ③ 精查（token info）：volume_5m/volume_1h >= 25% 且 5m涨幅>=+15% 且 1h量>=$5K
  命中 -> TG 推送「GMGN热搜」群（只推一次，seen 去重；只推送，不自动买入）

双端状态：cloud_vol_state.json（存于仓库），本地/云端双向同步去重；
          本地 60s 轮询先推，云端 5min 兜底接管（关机后）。

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
STATE_PATH = "cloud_vol_state.json"
GH_API = "https://api.github.com"
CHAINS = ["sol"]          # 只监控 SOL
HOT_INTERVAL = "1h"
LIMIT = 20
TARGET_CHAT = int(os.environ.get("TG_HOTSEARCH_CHAT_ID", "5499948080"))
TARGET_NAME = "GMGN热搜"

# ① 粗筛阈值（榜单字段，OR）
COARSE_HOT_LEVEL = 2        # hot_level >= 2
COARSE_SWAPS = 1000         # 1h swaps >= 1000
COARSE_CHG_1H = 100.0       # 1h 涨幅 >= 100%

# ② 质量闸门（AND，全部满足）
GATE_BOT_RATE = 0.55        # 机器人占比 <= 55%
GATE_LIQUIDITY = 50000.0    # 流动性 >= $50K
GATE_SMART = 3              # smart_degen >= 3
GATE_RUG = 0.30             # rug_ratio <= 0.3
GATE_NO_CTO = True          # 非 CTO 仿盘
GATE_DEV_CLOSE = True       # dev 已清仓

# ③ 精查阈值（token info）
SPIKE_RATIO = 0.25          # volume_5m / volume_1h >= 25%
SPIKE_CHG_5M = 15.0         # 5m 涨幅 >= +15%
MIN_VOL_1H = 5000.0         # 1h 成交 >= $5K


def gh_headers():
    return {
        "Authorization": "token " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
        "User-Agent": "gmgn-vol-spike-monitor",
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
    write_json_file(STATE_PATH, state, sha, "sync vol spike state from cloud [skip ci]")


def run_gmgn(args, timeout=180):
    for attempt in (1, 2):
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
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


def token_vol(addr):
    """token info 精查 -> dict(volume_5m, volume_1h, swaps_5m) 或 None"""
    args = ["gmgn-cli", "token", "info", "--chain", "sol", "--address", addr, "--raw"]
    try:
        out = run_gmgn(args, timeout=60)
        data = json.loads(out)
    except Exception as e:
        print("token info failed {}: {}".format(addr, e))
        return None
    price = data.get("price") or {}
    return {
        "volume_5m": float(price.get("volume_5m") or 0),
        "volume_1h": float(price.get("volume_1h") or 0),
        "swaps_5m": int(price.get("swaps_5m") or 0),
    }


def coarse_pass(t):
    hot = int(t.get("hot_level") or 0)
    swaps = int(t.get("swaps") or 0)
    chg1h = float(t.get("price_change_percent") or 0)
    if hot >= COARSE_HOT_LEVEL:
        return True, "hot_level={}>=2".format(hot)
    if swaps >= COARSE_SWAPS:
        return True, "swaps={}>={}".format(swaps, COARSE_SWAPS)
    if chg1h >= COARSE_CHG_1H:
        return True, "1h涨幅={:.0f}%>={}%".format(chg1h, COARSE_CHG_1H)
    return False, ""


def gate_pass(t):
    bot = t.get("bot_degen_rate")
    liq = float(t.get("liquidity") or 0)
    smart = int(t.get("smart_degen_count") or 0)
    rug = t.get("rug_ratio")
    cto = int(t.get("cto_flag") or 0)
    dev_close = bool(t.get("creator_close"))
    if bot is None or float(bot) > GATE_BOT_RATE:
        return False, "bot={} 超限".format(bot)
    if liq < GATE_LIQUIDITY:
        return False, "流动性=${:,.0f}<${:,.0f}".format(liq, GATE_LIQUIDITY)
    if smart < GATE_SMART:
        return False, "smart={}<{}".format(smart, GATE_SMART)
    if rug is None or float(rug) > GATE_RUG:
        return False, "rug={} 超限".format(rug)
    if GATE_NO_CTO and cto != 0:
        return False, "CTO仿盘(cto_flag={})".format(cto)
    if GATE_DEV_CLOSE and not dev_close:
        return False, "dev未清仓"
    return True, ""


def spike_pass(t, vol):
    if vol is None:
        return False, "精查数据缺失"
    v5, v1h = vol["volume_5m"], vol["volume_1h"]
    if v1h < MIN_VOL_1H:
        return False, "1h量 ${:,.0f} < ${:,.0f}".format(v1h, MIN_VOL_1H)
    ratio = v5 / v1h if v1h > 0 else 0
    if ratio < SPIKE_RATIO:
        return False, "5m占比 {:.1f}% < {}%".format(ratio * 100, SPIKE_RATIO * 100)
    chg5m = float(t.get("price_change_percent5m") or 0)
    if chg5m < SPIKE_CHG_5M:
        return False, "5m涨幅 {:.1f}% < +{}%".format(chg5m, SPIKE_CHG_5M)
    return True, ""


def _fmt_price(v):
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


def fmt_msg(t, vol):
    sym = t.get("symbol") or ""
    name = t.get("name") or ""
    addr = t.get("address") or ""
    lp = t.get("launchpad_platform") or ""
    v5 = vol["volume_5m"] if vol else 0
    v1h = vol["volume_1h"] if vol else 0
    ratio = v5 / v1h * 100 if vol and v1h > 0 else 0
    chg5m = round(float(t.get("price_change_percent5m") or 0), 1)
    chg1h = round(float(t.get("price_change_percent1h") or 0), 1)
    smart = int(t.get("smart_degen_count") or 0)
    bot = round(float(t.get("bot_degen_rate") or 0) * 100, 1)
    liq = round(float(t.get("liquidity") or 0))
    return "\n".join([
        "⚡ <b>GMGN 放量信号</b>  #{} {}".format(sym, name),
        "链: {} | 平台: {}".format("SOL", lp or "-"),
        "5m量: ${:,.0f} | 1h量: ${:,.0f} | 5m占比: {:.1f}%".format(v5, v1h, ratio),
        "5m涨跌: {}% | 1h涨跌: {}% | 价格 ${}".format(chg5m, chg1h, _fmt_price(t.get("price"))),
        "smart: {} | bot: {}% | 流动性: ${:,}".format(smart, bot, liq),
        "GMGN: https://gmgn.ai/sol/token/{}".format(addr),
        "<code>{}</code>".format(addr),
    ])


async def main():
    state, sha = read_state()
    print("cloud vol state: {} chains (seen {} sol)".format(
        len(state), len((state.get("sol") or {}).get("seen") or [])))

    client = TelegramClient(StringSession(os.environ["TG_STRING_SESSION"]),
                            int(os.environ["TG_API_ID"]),
                            os.environ["TG_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        print("FATAL: not authorized")
        sys.exit(1)
    peer = InputPeerChat(TARGET_CHAT)

    seen = set((state.get("sol") or {}).get("seen") or [])
    pushed = 0
    changed = False

    try:
        tokens = fetch_hot("sol")
    except Exception as e:
        print("[sol] fetch failed: {}".format(e))
        tokens = []
    if not tokens:
        print("[sol] empty rank")

    for t in tokens:
        addr = (t.get("address") or "").strip()
        sym = t.get("symbol") or ""
        if not addr:
            continue
        if addr in seen:
            continue  # 已推过

        ok, why = coarse_pass(t)
        if not ok:
            continue
        print("[sol] 粗筛命中: {} | {}".format(sym, addr))

        ok, why = gate_pass(t)
        if not ok:
            print("[sol] 闸门拦截: {} | {}".format(sym, why))
            continue

        vol = token_vol(addr)
        ok, why = spike_pass(t, vol)
        if not ok:
            print("[sol] 精查未达: {} | {}".format(sym, why))
            continue

        msg = fmt_msg(t, vol)
        print("[sol] ⚡放量命中: {} | {}".format(sym, addr))
        try:
            await client.send_message(peer, msg, parse_mode="html",
                                      link_preview=False)
            print("[sol] 已推送放量信号: {} | {}".format(sym, addr))
        except Exception as e:
            print("[sol] 推送失败 {}: {}".format(sym, e))
            continue
        seen.add(addr)
        pushed += 1
        changed = True

    # 写回状态（含 409 冲突重试）
    if changed:
        state["sol"] = {"seen": sorted(seen)}
        for attempt in range(1, 4):
            try:
                write_state(state, sha)
                print("cloud vol state written OK")
                break
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 409 and attempt < 3:
                    print("conflict, retry {}".format(attempt))
                    time.sleep(2)
                    state, sha = read_state()
                    merged_seen = set((state.get("sol") or {}).get("seen") or []) | seen
                    state["sol"] = {"seen": sorted(merged_seen)}
                    continue
                print("write failed: {}".format(e))
                break
            except Exception as e:
                print("write failed: {}".format(e))
                break
    else:
        print("no new spike, skip state write")

    print("本轮完成 扫描={} 推送={}（放量信号）".format(len(tokens), pushed))
    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print("FATAL: {}".format(e))
        sys.exit(1)
