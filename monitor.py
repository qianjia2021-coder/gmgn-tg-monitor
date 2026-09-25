#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GMGN 高频交易合约监控 -> Telegram 推送（云端版，GitHub Actions）
复刻本机 PowerShell 版逻辑：BSC / 5m / swaps 120-3000 / 成交额>=1万 / 24h 新代币
依赖：npm install -g gmgn-cli；环境变量 GMGN_API_KEY / TG_BOT_TOKEN / TG_CHAT_ID
去重：seen.json（仓库内，每次推送后立即写回；workflow 提交状态）
云端无 GUI：不打开浏览器，只推送 Telegram。
"""
import json
import os
import pathlib
import subprocess
import sys
import urllib.request

SEEN_FILE = os.environ.get("SEEN_FILE", "seen.json")
CHAIN = os.environ.get("CHAIN", "bsc")
INTERVAL = os.environ.get("INTERVAL", "5m")
MIN_SWAPS = int(os.environ.get("MIN_SWAPS", "120"))
MAX_SWAPS = int(os.environ.get("MAX_SWAPS", "3000"))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME", "10000"))
MAX_CREATED = os.environ.get("MAX_CREATED", "24h")
PLATFORM = os.environ.get("PLATFORM", "").strip().lower()
ADDR_SUFFIX = os.environ.get("ADDR_SUFFIX", "").strip().lower()

INTERVAL_MIN = {"1m": 1, "5m": 5, "1h": 60, "6h": 360, "24h": 1440}


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
        f"https://api.telegram.org/bot{tok}/sendMessage",
        data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def main():
    seen = load_seen()
    args = ["gmgn-cli", "market", "trending", "--chain", CHAIN,
            "--interval", INTERVAL, "--order-by", "swaps", "--limit", "100", "--raw"]
    if MAX_CREATED:
        args += ["--max-created", MAX_CREATED]
    print("cmd: " + " ".join(args))
    out = subprocess.run(args, capture_output=True, text=True,
                         timeout=180, check=True).stdout
    data = json.loads(out)
    rank = (data.get("data") or {}).get("rank") or []

    hits = []
    for t in rank:
        swaps = t.get("swaps") or 0
        volume = t.get("volume") or 0
        if not (MIN_SWAPS <= swaps <= MAX_SWAPS and volume >= MIN_VOLUME):
            continue
        if PLATFORM:
            lp = (t.get("launchpad_platform") or "").lower()
            if PLATFORM not in lp:
                continue
        if ADDR_SUFFIX:
            addr = (t.get("address") or "").lower()
            if not addr.endswith(ADDR_SUFFIX):
                continue
        hits.append(t)
    hits.sort(key=lambda x: (x.get("swaps") or 0), reverse=True)

    pushed = 0
    for t in hits:
        addr = (t.get("address") or "").strip()
        if not addr or addr in seen:
            continue
        sym = t.get("symbol") or ""
        name = t.get("name") or ""
        swaps = t.get("swaps") or 0
        freq_min = round(swaps / INTERVAL_MIN.get(INTERVAL, 5), 1)
        vol = round(float(t.get("volume") or 0))
        mc = round(float(t.get("market_cap") or 0))
        liq = round(float(t.get("liquidity") or 0))
        chg = round(float(t.get("price_change_percent") or 0), 1)
        price = t.get("price") or ""
        holders = t.get("holder_count") or ""
        bot_rate = round(float(t.get("bot_degen_rate") or 0) * 100, 1)
        lp = t.get("launchpad_platform") or ""
        msg = (
            f"<b>🔥 高频交易命中</b>  #{sym} {name}\n"
            f"链: {CHAIN} | 平台: {lp}\n"
            f"窗口 {INTERVAL} · <b>{swaps}</b> swaps ≈ {freq_min} 次/分钟\n"
            f"成交额: ${vol:,} | 市值: ${mc:,} | 流动性: ${liq:,}\n"
            f"价格: ${price} | {INTERVAL} 涨跌: {chg}%\n"
            f"持有人: {holders} | 机器人占比: {bot_rate}%\n"
            f"GMGN: https://gmgn.ai/{CHAIN}/token/{addr}\n"
            f"<code>{addr}</code>"
        )
        try:
            tg_send(msg)
            seen.add(addr)
            save_seen(seen)   # 推送成功立即落盘，保证每个合约只发一次
            pushed += 1
            print(f"pushed: {sym} | {addr}")
        except Exception as e:
            print(f"push failed for {sym}: {e}")

    print(f"hits={len(hits)} pushed={pushed} total_seen={len(seen)}")
    if pushed:
        save_seen(seen)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
