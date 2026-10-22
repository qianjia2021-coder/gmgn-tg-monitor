#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MooDeng 合约监听 - 云端版（GitHub Actions 接管）
================================================================
与本地 moodeng_auto_buy.py 同一策略（2026-10-22 18:18 起，workbuddy 版本）：
  - 只监听 web3_gxfc 群「SOL大厅」话题（TOPIC_ID = 23，群标题「恭喜发财」）
  - 单群直推：话题内 pump 后缀 + 30 分钟币龄内合约 → 立即 DBotX 模拟买 1 SOL（止盈 50%）
  - 一个合约只买一次（bought / seen / baseline 三重去重）
  - 买入成功/失败都推送「SOL推送」群（私有邀请链接 CheckChatInvite 解析）
  - 不查 KOL、不用黑名单拦截（与本地一致）
触发时机：本机心跳 heartbeat_moodeng.txt 距今 >120s（关机/断线）时，由
          moodeng-monitor workflow 每 5 分钟接管；本机在线时 workflow 跳过，本脚本不运行。
状态文件：cloud_moodeng_state.json（seen/baseline）+ cloud_moodeng_bought.json（bought）
Env:
  MOODENG_TG_API_ID / MOODENG_TG_API_HASH / MOODENG_TG_STRING_SESSION
  DBOTX_API_KEY / GITHUB_TOKEN（Actions 自动提供）
"""
import asyncio, base64, json, os, re, sys, time
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

REPO = "qianjia2021-coder/gmgn-tg-monitor"
STATE_PATH = "cloud_moodeng_state.json"
BOUGHT_PATH = "cloud_moodeng_bought.json"
GH_API = "https://api.github.com"

# ==== 与本地 moodeng_auto_buy.py 对齐的策略配置 ====
TARGET = "web3_gxfc"            # 只监听这个群
TOPIC_ID = 23                   # 该群「SOL大厅」话题 id（链接末尾数字）
NOTIFY_TARGET = "https://t.me/+QfV7aMQ05B02ODE9"   # 推送群（私有邀请链接）
MAX_TOKEN_AGE_MIN = 30          # 只买 30 分钟币龄内的合约
ONLY_PUMP_SUFFIX = True
PUMP_SUFFIX = "pump"
BUY_SOL = 1.0
STOP_EARN = 0.5
SLIPPAGE = 0.5
MSG_WINDOW_SEC = 900            # 只处理最近 15 分钟内的消息（覆盖关机→云端接管间隙）
SEEN_LIMIT = 20000              # seen 上限，防 state 无限膨胀
# =====================================================

DBOTX_SWAP_URL = "https://api-bot-v1.dbotx.com/simulator/sim_swap_order"
DBOTX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

SOL_NATIVE = "So11111111111111111111111111111111111111112"
ADDR_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
AGE_RE = re.compile(r"\u231b\ufe0f?\s*(\d+)\s*(m|h)(?:\s*(\d+)\s*m)?")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S") + " " + str(msg), flush=True)


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
    for attempt in range(2):
        r = requests.put(f"{GH_API}/repos/{REPO}/contents/{path}", headers=gh_headers(), json=body, timeout=30)
        if r.status_code == 409 and attempt == 0:
            time.sleep(2)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("write_json_file failed: " + path)


# ---------------- 地址/币龄/后缀过滤（与本地完全一致） ----------------
def extract_addrs_raw(text):
    out = []
    for cand in ADDR_RE.findall(text or ""):
        if cand != SOL_NATIVE:
            out.append(cand)
    return out


def parse_token_age_min(text):
    m = AGE_RE.search(text or "")
    if not m:
        return None
    num = int(m.group(1))
    unit = m.group(2)
    extra = m.group(3)
    if unit == "h":
        return num * 60 + (int(extra) if extra else 0)
    return num


def extract_addrs(text):
    addrs = extract_addrs_raw(text)
    if ONLY_PUMP_SUFFIX:
        addrs = [a for a in addrs if a.lower().endswith(PUMP_SUFFIX)]
    return addrs


def msg_topic_id(m):
    rt = getattr(m, "reply_to", None)
    if rt is not None:
        tid = getattr(rt, "reply_to_top_id", None)
        if tid:
            return int(tid)
        fid = getattr(rt, "reply_to_msg_id", None)
        if fid:
            return int(fid)
    rid = getattr(m, "reply_to_msg_id", None)
    return int(rid) if rid else None


# ---------------- 推送目标解析（与本地一致：CheckChatInvite / ImportChatInvite） ----------------
def invite_hash(link):
    s = (link or "").strip()
    if "/+" in s:
        return s.split("/+", 1)[1].split("/")[0].split("?")[0].strip()
    if "joinchat/" in s:
        return s.split("joinchat/", 1)[1].split("/")[0].split("?")[0].strip()
    return None


async def resolve_notify_target(client):
    h = invite_hash(NOTIFY_TARGET)
    if h:
        try:
            from telethon.tl.functions.messages import (
                CheckChatInviteRequest, ImportChatInviteRequest)
            from telethon.tl.types import ChatInviteAlready
            res = await client(CheckChatInviteRequest(hash=h))
            if isinstance(res, ChatInviteAlready):
                return res.chat
            log("推送群尚未加入，尝试加入...")
            upd = await client(ImportChatInviteRequest(hash=h))
            for c in getattr(upd, "chats", None) or []:
                return c
        except Exception as e:
            log("推送目标解析失败: {}: {}".format(type(e).__name__, e))
        log("!! 推送目标解析失败，本轮不推送（目标：{}）".format(NOTIFY_TARGET))
        return None
    try:
        return await client.get_entity(NOTIFY_TARGET)
    except Exception as e:
        log("推送目标 get_entity 失败: {}".format(e))
        return None


# ---------------- DBotX 模拟买入（与本地 api_buy 一致） ----------------
def api_buy(addr):
    key = os.environ.get("DBOTX_API_KEY", "")
    if not key:
        return False, "no DBOTX_API_KEY"
    body = {"chain": "solana", "pair": addr, "type": "buy",
            "amountOrPercent": BUY_SOL, "stopEarnPercent": STOP_EARN,
            "stopLossPercent": None, "priorityFee": "", "gasFeeDelta": 5,
            "maxFeePerGas": 100, "walletId": "", "slippage": SLIPPAGE}
    headers = {"X-API-KEY": key, "Content-Type": "application/json",
               "accept": "application/json", "User-Agent": DBOTX_UA,
               "Origin": "https://dbotx.com", "Referer": "https://dbotx.com/"}
    for attempt in range(5):
        try:
            r = requests.post(DBOTX_SWAP_URL, json=body, headers=headers, timeout=30)
            d = r.json()
            if d.get("err") is False:
                return True, d.get("res", {}).get("id", "")
            res_str = str(d)
            if "pair not found" in res_str:
                return False, res_str[:200]
            if r.status_code >= 500 and attempt < 4:
                time.sleep(3)
                continue
            return False, res_str[:200]
        except Exception as e:
            if attempt < 4:
                time.sleep(2)
                continue
            return False, str(e)
    return False, "retry exhausted"


async def main():
    state, sha = read_json_file(STATE_PATH)
    bought, bsha = read_json_file(BOUGHT_PATH)
    seen = state.get("seen") or {}
    # baseline 兼容 dict / list 两种历史格式
    baseline_raw = state.get("baseline") or {}
    if isinstance(baseline_raw, list):
        baseline = {a: "baseline" for a in baseline_raw}
    else:
        baseline = dict(baseline_raw)
    log("state: seen={} baseline={} bought={}".format(len(seen), len(baseline), len(bought)))

    client = TelegramClient(StringSession(os.environ["MOODENG_TG_STRING_SESSION"]),
                            int(os.environ["MOODENG_TG_API_ID"]),
                            os.environ["MOODENG_TG_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        log("FATAL: TG 会话未授权")
        sys.exit(1)
    me = await client.get_me()
    log("已登录 TG: {}".format(getattr(me, "username", "") or getattr(me, "first_name", "")))

    notify = await resolve_notify_target(client)

    try:
        peer = await client.get_entity(TARGET)
    except Exception as e:
        log("群 @{} 访问失败: {}（云端账号可能不在该群）".format(TARGET, e))
        await client.disconnect()
        sys.exit(2)
    cname = getattr(peer, "title", TARGET)
    log("监听群: {} (@{}) 话题={}".format(cname, TARGET, TOPIC_ID))

    now = time.time()
    new_seen = {}
    new_bought = {}
    processed = 0
    async for m in client.iter_messages(peer, limit=200):
        if m.date is None or m.date.timestamp() < now - MSG_WINDOW_SEC:
            continue
        if msg_topic_id(m) != TOPIC_ID:
            continue
        text = m.message or ""
        addrs = extract_addrs(text)
        if not addrs:
            continue
        delay_min = (now - m.date.timestamp()) / 60.0
        for addr in addrs:
            if addr in bought or addr in seen or addr in baseline:
                continue
            processed += 1
            # 币龄：文本年龄 + 消息延迟（云端是轮询，必须换算实际年龄）
            age = parse_token_age_min(text)
            real_age = (age + delay_min) if age is not None else None
            if real_age is not None and real_age > MAX_TOKEN_AGE_MIN:
                log("  币龄超限(实际 {:.0f} 分钟 > {})，跳过: {}".format(
                    real_age, MAX_TOKEN_AGE_MIN, addr[:12] + "..."))
                new_seen[addr] = time.strftime("%Y-%m-%d %H:%M:%S")
                continue
            log("new [{}] #{}: {} (delay={:.0f}s)".format(cname, m.id, addr, (now - m.date.timestamp())))
            bok, info = api_buy(addr)
            rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "order_id": info if bok else "", "sol": BUY_SOL,
                   "stop_earn": STOP_EARN, "groups": [cname], "ok": bok,
                   "src": "cloud"}
            if not bok:
                rec["err"] = str(info)[:200]
            new_bought[addr] = rec
            new_seen[addr] = rec["ts"]
            log("  {} -> {} | {}".format("买入成功" if bok else "买入失败", addr, info))
            # 推送（成功/失败都推，与本地一致）
            if notify is not None:
                status = "✅买入成功" if bok else "❌买入失败"
                msg = "{}\n合约: `{}`\n单群直推: {}\n订单: {}\n时间: {}".format(
                    status, addr, cname, info if bok else "无", rec["ts"])
                try:
                    await client.send_message(notify, msg)
                    log("  通知已推送")
                except Exception as e:
                    log("  通知发送失败: {}".format(e))
            await asyncio.sleep(2)
    await client.disconnect()

    if processed:
        log("本轮处理新合约 {} 个（其余为已见/非本话题/超龄）".format(processed))

    # 写回状态
    if new_seen or new_bought:
        bought.update(new_bought)
        seen.update(new_seen)
        # seen 裁剪：保留最近 SEEN_LIMIT 条（字典插入序，新条目在后）
        if len(seen) > SEEN_LIMIT:
            drop = len(seen) - SEEN_LIMIT
            for k in list(seen.keys())[:drop]:
                del seen[k]
        state["seen"] = seen
        state["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            write_json_file(STATE_PATH, state, sha, "sync moodeng state from cloud [skip ci]")
            write_json_file(BOUGHT_PATH, bought, bsha, "sync moodeng bought from cloud [skip ci]")
            log("状态已写回: bought+{} seen+{}".format(len(new_bought), len(new_seen)))
        except Exception as e:
            log("写回失败: {}".format(e))
    log("done")


if __name__ == "__main__":
    asyncio.run(main())
