"""
云端监控 AveBSCTokenScanner_cn：新币开盘 + 尾数7777 + Dex=Pancakeclamm
- GitHub Actions 每5分钟跑一次
- 先检查 heartbeat.txt，5分钟内有心跳说明本机在跑，直接退出
- 心跳超时则拉取最近5分钟消息，命中推送到 BSC推送群
- 无去重，命中就推
"""
import asyncio
import base64
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = 39227045
API_HASH = "cd451ed24a3226f46bfd730ff4d8b5dd"
STRING_SESSION = "1BVtsOLMBu7LflS9vXBUG7QMlcramBuL2x2ujWazgbxYIblcRen-vY803xlyKFAvXS6mU_bTfgMMq7Do5piFaLPjWUPbxNKyA9bCqGsrvAJv1MpZ5bc4pJwrjlUZZPM0N1BoshLtaq1Gbk-0YO5Z6KA0bOB8mbp79-AwUarszeQILrR8dVKy86NP9dxIXPhakMhqbIyVOuBfxjsaQPRg97ELMfj5v5l3DJjNVbnk3T5wU_SmphD_qi4ht_UiwfsWEP8Ijo_RMMoXcOvHDcopO2tpjesWGZKMuAS4ZWG0gfjlb_5iFQuH9RinDUTF2QbyNda-I6xNn9BVGYh2uahPLdkxQ2UstG_g="

BOT_TOKEN = "8830221408:AAERiig973Vi8YK5pebxO_S3iKsapX91PTc"
TARGET_CHAT_ID = -5194908956
SOURCE = "AveBSCTokenScanner_cn"
SUFFIX = "7777"

REPO = "qianjia2021-coder/gmgn-tg-monitor"
TOKEN = os.environ.get("GITHUB_TOKEN", "")

DBOTX_API_KEY = "crxzqlhl60iybwilnugutg6s2oabsb12"
DBOTX_BUY_URL = "https://servapi.dbotx.com/simulator/sim_swap_order"


def log(msg):
    print(f"[{datetime.now().isoformat()}] {msg}", flush=True)


def check_heartbeat() -> bool:
    """检查本机心跳文件，5分钟内有心跳返回 True（本机在跑，云端退出）。"""
    if not TOKEN:
        log("no GITHUB_TOKEN env, skip heartbeat check")
        return False
    url = f"https://api.github.com/repos/{REPO}/contents/heartbeat_ave.txt"
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            log(f"heartbeat file not found (status {r.status_code}), cloud takes over")
            return False
        data = r.json()
        content = base64.b64decode(data["content"]).decode().strip()
        log(f"heartbeat content: {content}")
        # 解析 ISO 时间
        hb_time = datetime.fromisoformat(content.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = (now - hb_time).total_seconds()
        log(f"heartbeat age: {diff:.0f}s")
        if diff < 300:
            log("local machine is alive (<5min), cloud exits")
            return True
        else:
            log("local machine offline (>5min), cloud takes over")
            return False
    except Exception as e:
        log(f"heartbeat check error: {e}, cloud takes over")
        return False


def bot_send(text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": TARGET_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"},
            timeout=20,
        )
        return r.json().get("ok", False)
    except Exception as e:
        log(f"bot_send error: {e}")
        return False


def dbotx_buy(contract: str):
    payload = {
        "pair": contract,
        "slippage": 0.1,
        "chain": "bsc",
        "amountOrPercent": "1",
        "type": "buy",
        "stopEarnPercent": 0.5,
        "stopLossPercent": "",
        "stopEarnGroup": None,
        "stopLossGroup": None,
    }
    headers = {"X-API-Key": DBOTX_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(DBOTX_BUY_URL, json=payload, headers=headers, timeout=30)
        data = r.json()
        if not data.get("err", True):
            log(f"dbotx BUY OK: {contract} order={data.get('res',{}).get('id','?')}")
        else:
            log(f"dbotx BUY FAIL: {contract} resp={r.text[:200]}")
    except Exception as e:
        log(f"dbotx BUY error: {e}")


def gmgn_has_safety_unknown(contract: str) -> bool:
    """open_timestamp=0 即为安全未知。"""
    try:
        r = requests.post(
            "https://gmgn.ai/api/v1/mutil_window_token_info?chain=bsc",
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json", "Referer": "https://gmgn.ai/"},
            json={"chain": "bsc", "type": "token_address", "addresses": [contract]},
            timeout=15,
        )
        token = r.json().get("data", [{}])[0]
        return token.get("open_timestamp", 1) == 0
    except Exception as e:
        log(f"gmgn check error: {e}, assume safe")
        return False


def parse_open_message(text: str) -> dict | None:
    if "新币开盘" not in text:
        return None
    m = re.search(r"合约:\s*(0x[a-fA-F0-9]{40})", text)
    if not m:
        return None
    contract = m.group(1)
    name = ""
    m2 = re.search(r"代币:\s*(.+)", text)
    if m2:
        name = m2.group(1).strip()
    dex = ""
    m3 = re.search(r"Dex:\s*(.+)", text)
    if m3:
        dex = m3.group(1).strip()
    liq = ""
    m4 = re.search(r"流动性:\s*([^\n]+)", text)
    if m4:
        liq = m4.group(1).strip()
    return {"contract": contract, "name": name, "dex": dex, "liq": liq}


async def main():
    if check_heartbeat():
        return

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        log("NOT AUTHORIZED")
        return

    ent = await client.get_entity(SOURCE)
    # 拉最近 8 分钟消息（留余量）
    since = datetime.now(timezone.utc) - timedelta(minutes=8)
    hits = 0
    async for msg in client.iter_messages(ent, limit=200):
        if msg.date < since:
            break
        text = msg.message or ""
        info = parse_open_message(text)
        if not info:
            continue
        if not info["contract"].lower().endswith(SUFFIX):
            continue
        dex_lower = info["dex"].lower()
        is_clamm = "clamm" in dex_lower
        is_cake_v2 = "cake" in dex_lower and not is_clamm
        if not (is_clamm or is_cake_v2):
            continue
        if is_cake_v2 and gmgn_has_safety_unknown(info["contract"]):
            log(f"SKIP: {info['contract']} has GMGN safety unknown")
            continue
        hits += 1
        log(f"HIT: {info['contract']} {info['name']} {info['dex']} {info['liq']}")
        msg_text = (
            f"尾数 {SUFFIX} 命中（云端）\n"
            f"<code>{info['contract']}</code>\n"
            f"{info['name']}\n"
            f"{info['dex']}\n"
            f"流动性: {info['liq']}"
        )
        bot_send(msg_text)
        dbotx_buy(info["contract"])

    log(f"done, {hits} hits")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

