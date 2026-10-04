#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BSC Alerts 二次命中监控 - 云端版（GitHub Actions，每 5 分钟）
=============================================================
- 拉一撇/橙子最近消息（种子源，不做平台过滤）
- 拉 BSC Alerts 文件夹群最近消息（只统计 Flap/Four 平台）
- 状态存 GitHub repo（cloud_bsc_state.json），与本地双向合并
- 触发：count>=2 且 sources 含一撇/橙子 → 发 TG + dbotx 买 0.1 BNB 止盈50%
- 云端不开浏览器（debot.ai 标签仅本地开）
"""
import asyncio, base64, json, os, re, time
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetDialogFiltersRequest

API_ID = int(os.environ.get("TG_API_ID", "39227045"))
API_HASH = os.environ.get("TG_API_HASH", "cd451ed24a3226f46bfd730ff4d8b5dd")
STRING_SESSION = os.environ["TG_STRING_SESSION"]
TARGET_CHAT_ID = int(os.environ.get("TG_TARGET_CHAT_ID", "-5194908956"))
FOLDER_NAME = "BSC Alerts"
SEED_GROUPS = ["FindTheGoldenDoge", "chengzi_golden"]
SCAN_LIMIT = 50   # 每群扫最近 N 条

REPO = "qianjia2021-coder/gmgn-tg-monitor"
STATE_PATH = "cloud_bsc_state.json"
GH_API = "https://api.github.com"

ADDR_RE = re.compile(r"(?<![a-fA-F0-9])0x[a-fA-F0-9]{40}(?![a-fA-F0-9])")
ZERO = "0x0000000000000000000000000000000000000000"
SKIP = {ZERO, "0x10ed43c718714eb63d5aa57b78b54704e256024e",
        "0xca143ce32fe787f7f3def1387fb428719f23447"}

DBOTX_SWAP_URL = "https://api-bot-v1.dbotx.com/simulator/sim_swap_order"
DBOTX_API_KEY = os.environ.get("DBOTX_API_KEY", "b6u3i891nbhigvwyqhj5h1o89d34wihq")
DBOTX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)


def gh_headers():
    return {"Authorization": "token " + os.environ["GITHUB_TOKEN"],
            "Accept": "application/vnd.github+json", "User-Agent": "bsc-alerts-monitor"}

def read_state():
    r = requests.get(f"{GH_API}/repos/{REPO}/contents/{STATE_PATH}", headers=gh_headers(), timeout=20)
    if r.status_code == 404:
        return {"count": {}, "sources": {}, "pushed": {}}, None
    r.raise_for_status()
    j = r.json()
    return json.loads(base64.b64decode(j["content"]).decode()), j["sha"]

def write_state(st, sha):
    payload = json.dumps(st, ensure_ascii=False).encode("utf-8")
    body = {"message": "sync bsc alerts state [skip ci]",
            "content": base64.b64encode(payload).decode(), "branch": "main"}
    if sha: body["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{REPO}/contents/{STATE_PATH}",
                     headers=gh_headers(), json=body, timeout=30)
    r.raise_for_status()

def extract_addrs(text):
    if not text: return []
    out = []
    for a in ADDR_RE.findall(text):
        al = a.lower()
        if al in SKIP or al in out: continue
        out.append(al)
    return out

def is_target_platform(text):
    if not text: return False
    low = text.lower()
    m = re.search(r"launch:\s*([^\n·]+)", low)
    if m and ("flap" in m.group(1) or "four" in m.group(1)): return True
    if "fourmeme" in low or "four.meme" in low: return True
    return False

def extract_name(text, contract):
    if not text: return ""
    lines = text.split("\n")
    def clean(line):
        line = line.strip()
        line = re.sub(r"\s*\|\s*#\w+.*$", "", line).strip()
        line = re.sub(r"^[#\s►▶\-•]+", "", line).strip()
        line = re.sub(r"^(Name|名称|Token|币名)\s*[:：]\s*", "", line, flags=re.I).strip()
        return line
    for i, line in enumerate(lines):
        if contract in line and ("CA" in line or "ca" in line.lower()):
            if i+1 < len(lines):
                n = clean(lines[i+1])
                if n and len(n) <= 60 and not n.startswith("0x"): return n
    for line in lines:
        m = re.match(r"^\s*(?:Name|名称|Token|币名)\s*[:：]\s*(.+)$", line, re.I)
        if m:
            n = clean(m.group(1))
            if n and len(n) <= 60 and not n.startswith("0x"): return n
    for line in lines:
        n = clean(line)
        if not n: continue
        if n.startswith("0x") or n.startswith("http") or n.startswith("@"): continue
        if re.match(r"^[\W_]+$", n): continue
        if len(n) <= 60: return n
    return ""

def dbotx_buy(contract):
    body = {"chain": "bsc", "pair": contract, "walletId": "", "type": "buy",
            "amountOrPercent": 0.1, "stopEarnPercent": 0.5, "stopLossPercent": None,
            "priorityFee": "", "gasFeeDelta": 5, "maxFeePerGas": 100, "slippage": 0.5}
    r = requests.post(DBOTX_SWAP_URL, json=body,
                      headers={"X-API-KEY": DBOTX_API_KEY, "Content-Type": "application/json",
                               "accept": "application/json", "User-Agent": DBOTX_UA,
                               "Origin": "https://dbotx.com", "Referer": "https://dbotx.com/"}, timeout=30)
    try:
        d = r.json()
        if d.get("err") is False: return True, d.get("res", {}).get("id", "")
        return False, str(d)[:200]
    except Exception as e:
        return False, f"HTTP {r.status_code} {e}"

async def get_folder_chats():
    res = await client(GetDialogFiltersRequest())
    for f in getattr(res, "filters", res):
        t = getattr(f, "title", None)
        if hasattr(t, "text"): t = t.text
        if t == FOLDER_NAME:
            return list(getattr(f, "pinned_peers", []) or []) + \
                   [p for p in (getattr(f, "include_peers", []) or [])
                    if p not in (getattr(f, "pinned_peers", []) or [])]
    return []

async def main():
    await client.connect()
    st, sha = read_state()
    st.setdefault("count", {})
    st.setdefault("sources", {})
    st.setdefault("pushed", {})

    peers = await get_folder_chats()
    chats = []
    for p in peers:
        try: chats.append(await client.get_entity(p))
        except Exception: pass
    for u in SEED_GROUPS:
        try: chats.append(await client.get_entity(u))
        except Exception: pass

    triggered = []
    for ent in chats:
        title = getattr(ent, "title", "?")
        is_seed = ("一撇" in title) or ("橙子" in title)
        try:
            async for msg in client.iter_messages(ent, limit=SCAN_LIMIT):
                text = msg.message or ""
                if msg.reply_to is not None: continue  # 跳过机器人回复
                # 所有群都计数，不再过滤平台
                for c in extract_addrs(text):
                    if msg.date and (time.time() - msg.date.timestamp() > 1800): continue
                    st["sources"].setdefault(c, [])
                    if title in st["sources"][c]: continue  # 同群同合约只算一次
                    st["count"][c] = st["count"].get(c, 0) + 1
                    st["sources"][c].append(title)
                    has_seed = any(("一撇" in s) or ("橙子" in s) for s in st["sources"][c])
                    if st["count"][c] >= 2 and has_seed and not st["pushed"].get(c):
                        st["pushed"][c] = True
                        triggered.append((c, title, text))
        except Exception as e:
            print(f"scan {title} err: {e}")

    # 触发推送
    for c, src, text in triggered:
        name = extract_name(text, c)
        import html as H
        name_line = f"名称: <b>{H.escape(name)}</b>\n" if name else ""
        msg = (f"🚀 <b>BSC Alerts 二次命中（云端）</b>\n{name_line}"
               f"合约: <code>{c}</code>\n来源: {H.escape(src)}\n已自动买入 0.1 BNB，止盈 +50% 全卖")
        try:
            await client.send_message(TARGET_CHAT_ID, msg, parse_mode="html", link_preview=False)
            print(f"pushed {c}")
        except Exception as e:
            print(f"push fail {c}: {e}")
        ok, info = dbotx_buy(c)
        print(f"dbotx {c}: ok={ok} {info}")

    write_state(st, sha)
    print(f"done. triggered={len(triggered)}, total contracts={len(st['count'])}")
    await client.disconnect()

asyncio.run(main())
