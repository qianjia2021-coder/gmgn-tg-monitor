#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""速通币扫描 -> Telegram 推送（云端版，GitHub Actions）
与本地 speedrun_scan.py 同一画像（SOL / 5m swaps>=500 / 聪明钱>=5 / 低bundler / 已毕业 / 不排除pump尾缀）
推送：TG_BOT_TOKEN -> TG_CHAT_ID（bot）
去重：seen.json（仓库内，推送后立即写回，workflow commit）
"""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

SEEN_FILE = "seen.json"
CHAIN = "sol"
INTERVAL = "5m"
LIMIT = 100
MIN_SWAPS = 500
MIN_VOLUME = 50000.0
MIN_SMART = 5
MAX_BUNDLER = 0.60
MAX_BOT = 0.80
MAX_RUG = 0.20
MAX_TOP10 = 0.30
MAX_AGE_HOURS = 24
EXCLUDE_ADDR_SUFFIX = ""   # 不排除 pump 尾缀（与本地一致）


def load_seen():
    p = pathlib.Path(SEEN_FILE)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(str(x).strip() for x in data if isinstance(x, str) and x.strip())
        except Exception:
            pass
    return set()


def save_seen(s):
    pathlib.Path(SEEN_FILE).write_text(
        json.dumps(sorted(s), ensure_ascii=False, indent=1), encoding="utf-8")


def tg_send(text):
    tok = os.environ["TG_BOT_TOKEN"]
    chat = os.environ["TG_CHAT_ID"]
    data = json.dumps({
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.telegram.org/bot{}/sendMessage".format(tok),
        data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def run_gmgn(args):
    """执行 gmgn-cli；429 限频时等待 35s 重试一次"""
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


def is_graduated(t):
    status = str(t.get("launchpad_status") or "0")
    exchange = (t.get("exchange") or "").lower()
    if status not in ("1", "2"):
        return False
    if exchange == "pump":
        return False
    return True


def pass_filters(t):
    addr = (t.get("address") or "").lower()
    if not addr or (EXCLUDE_ADDR_SUFFIX and addr.endswith(EXCLUDE_ADDR_SUFFIX)):
        return False
    if not is_graduated(t):
        return False
    created = int(t.get("creation_timestamp") or 0)
    if created and (time.time() - created) > MAX_AGE_HOURS * 3600:
        return False
    if int(t.get("swaps") or 0) < MIN_SWAPS:
        return False
    if float(t.get("volume") or 0) < MIN_VOLUME:
        return False
    if int(t.get("smart_degen_count") or 0) < MIN_SMART:
        return False
    if float(t.get("bundler_rate") or 0) > MAX_BUNDLER:
        return False
    if float(t.get("bot_degen_rate") or 0) > MAX_BOT:
        return False
    if float(t.get("rug_ratio") or 0) > MAX_RUG:
        return False
    if float(t.get("top_10_holder_rate") or 0) > MAX_TOP10:
        return False
    return True


def fmt_msg(t):
    sym = t.get("symbol") or ""
    name = t.get("name") or ""
    addr = t.get("address") or ""
    swaps = int(t.get("swaps") or 0)
    vol = round(float(t.get("volume") or 0))
    mc = round(float(t.get("market_cap") or 0))
    liq = round(float(t.get("liquidity") or 0))
    holders = t.get("holder_count") or 0
    smart = t.get("smart_degen_count") or 0
    kol = t.get("renowned_count") or 0
    bot = round(float(t.get("bot_degen_rate") or 0) * 100, 1)
    bundler = round(float(t.get("bundler_rate") or 0) * 100, 1)
    rug = float(t.get("rug_ratio") or 0)
    chg5 = round(float(t.get("price_change_percent") or 0), 1)
    chg1h = round(float(t.get("price_change_percent1h") or 0), 1)
    price = t.get("price") or 0
    lp = t.get("launchpad_platform") or ""
    lines = [
        "<b>速通币命中</b>  #{} {}".format(sym, name),
        "链: SOL | 平台: {} | 状态: 已毕业".format(lp),
        "5m: <b>{}</b> swaps | 成交额 ${:,} | 5m涨跌 {}% | 1h涨跌 {}%".format(
            swaps, vol, chg5, chg1h),
        "市值 ${:,} | 流动性 ${:,} | 持有人 {:,}".format(mc, liq, int(holders)),
        "聪明钱 {} | KOL {} | bot {:.1f}% | bundler {:.1f}% | rug {:.2f}".format(
            smart, kol, bot, bundler, rug),
        "价格: ${}".format(price),
        "GMGN: https://gmgn.ai/sol/token/{}".format(addr),
        "debot: https://debot.ai/token/solana/{}".format(addr),
        "<code>{}</code>".format(addr),
    ]
    return "\n".join(lines)


def main():
    seen = load_seen()
    args = ["gmgn-cli", "market", "trending", "--chain", CHAIN,
            "--interval", INTERVAL, "--limit", str(LIMIT), "--raw"]
    print("cmd: " + " ".join(args))
    out = run_gmgn(args)
    data = json.loads(out)
    rank = (data.get("data") or {}).get("rank") or []
    hits = [t for t in rank if pass_filters(t)]
    hits.sort(key=lambda x: int(x.get("swaps") or 0), reverse=True)
    print("hits={}".format(len(hits)))
    pushed = 0
    for t in hits:
        addr = (t.get("address") or "").strip()
        if not addr or addr in seen:
            continue
        try:
            tg_send(fmt_msg(t))
            seen.add(addr)
            save_seen(seen)
            pushed += 1
            print("pushed: {} | {}".format(t.get("symbol") or "", addr))
        except Exception as e:
            print("push failed for {}: {}".format(t.get("symbol") or "", e))
    print("pushed={} total_seen={}".format(pushed, len(seen)))
    if pushed:
        save_seen(seen)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FATAL: {}".format(e))
        sys.exit(1)
