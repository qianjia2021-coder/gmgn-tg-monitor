#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KOL 买入盯盘 -> Telegram 推送（云端版，GitHub Actions）
监控：zetman.opcat / Lowskii 两个 KOL 钱包，一有新的买入（buy）即推送。
数据：gmgn-cli portfolio activity --type buy（最近 360s 窗口）
推送：TG Bot API（环境变量 TG_BOT_TOKEN / TG_CHAT_ID）
去重：seen_kol.json（仓库内，workflow 提交状态）
"""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

SEEN_FILE = os.environ.get("SEEN_FILE", "seen_kol.json")
BUY_WINDOW = int(os.environ.get("BUY_WINDOW", "360"))
MIN_USD = int(os.environ.get("MIN_USD", "100"))
CHAIN = os.environ.get("CHAIN", "sol")

KOLS = [
    {"wallet": "DexpA8dcqNN9x9tvHw7wBzt43LUnp1cfWebV9t84kFEj",
     "name": "zetman.opcat", "tw": "@zetman_eth"},
    {"wallet": "41uh7g1DxYaYXdtjBiYCHcgBniV9Wx57b7HU7RXmx1Gg",
     "name": "Lowskii", "tw": "@Lowskii"},
]


def load_seen():
    p = pathlib.Path(SEEN_FILE)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_seen(s):
    pathlib.Path(SEEN_FILE).write_text(
        json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


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


def fetch_recent_buys(wallet):
    args = ["gmgn-cli", "portfolio", "activity", "--chain", CHAIN,
            "--wallet", wallet, "--type", "buy", "--limit", "50", "--raw"]
    out = subprocess.run(args, capture_output=True, text=True,
                         timeout=120, check=True).stdout
    data = json.loads(out)
    acts = data.get("activities") or []
    now = time.time()
    rows = []
    for a in acts:
        ts = int(a.get("timestamp") or 0)
        if now - ts > BUY_WINDOW:
            continue
        tok = a.get("token") or {}
        addr = tok.get("address") or ""
        if not addr:
            continue
        rows.append({
            "addr": addr, "sym": tok.get("symbol") or "",
            "ts": ts, "usd": round(float(a.get("cost_usd") or 0)),
        })
    return rows


def fmt_msg(kol, row):
    addr = row["addr"]
    return (
        f"<b>🧠 KOL 买入</b>  #{row.get('sym') or ''}\n"
        f"KOL: {kol['name']} {kol['tw']}\n"
        f"金额: ${row['usd']:,} | "
        f"{time.strftime('%m-%d %H:%M', time.localtime(row['ts']))}\n"
        f"GMGN: https://gmgn.ai/sol/token/{addr}\n"
        f"debot: https://debot.ai/token/solana/{addr}\n"
        f"<code>{addr}</code>"
    )


def scan_once(seen):
    hits = []
    for kol in KOLS:
        try:
            rows = fetch_recent_buys(kol["wallet"])
        except Exception as e:
            print(f"kol scan fail {kol['name']}: {e}")
            continue
        for row in rows:
            key = f"{kol['wallet'][:8]}:{row['addr']}"
            if key in seen:
                continue
            if row["usd"] < MIN_USD:
                seen[key] = str(row["ts"])
                print(f"kol skip <${MIN_USD}: {kol['name']} {row['sym']} ${row['usd']}")
                continue
            hits.append((kol, row, key))
            seen[key] = str(row["ts"])
    pushed = 0
    for kol, row, key in hits:
        try:
            tg_send(fmt_msg(kol, row))
            pushed += 1
            print(f"kol pushed: {kol['name']} {row['sym']} {row['addr']}")
        except Exception as e:
            print(f"kol push failed {kol['name']} {row['sym']}: {e}")
    save_seen(seen)
    print(f"kol hits={len(hits)} pushed={pushed} total_seen_kol={len(seen)}")


def main():
    seen = load_seen()
    scan_once(seen)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"KOL FATAL: {e}")
        sys.exit(1)
