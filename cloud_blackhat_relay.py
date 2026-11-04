#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Blackhat SB GMGN -> GMGN热搜  转发服务（云端 GitHub Actions 短任务版）
=====================================================================
源群   : t.me/gmgn100x            (Channel id 2378931267)
目标群 : t.me/+wPUznBRSqVkwYzE1   (Chat id -5499948080,  GMGN热搜)

双端无缝接力：
  - 本机 tg_relay2.py 长驻运行，每 60 秒把完整状态（last_id/counts/pushed/hist）
    写入仓库 blackhat_heartbeat.json，identity=local
  - 本脚本由 GitHub Actions 每 5 分钟触发：
      1. 读 blackhat_heartbeat.json；若 ts 在 HEARTBEAT_FRESH_SECONDS 内且 identity=local
         → 本机在线，直接退出不干活
      2. 否则本机关机/断网，云端接管：
         a. 从心跳恢复 last_id / counts / pushed / hist
         b. 用 StringSession 连 TG（与本机 .session 是不同 auth key，互不冲突）
         c. 拉取 min_id=last_id 的新消息，处理多轮直到无新消息
         d. 累计达到 threshold(=3) 的合约转发到目标群，parse_mode="html"，地址包 <code>
         e. 跨机去重：读目标群最近 500 条，已转发过的合约跳过
         f. 更新 blackhat_heartbeat.json，identity=cloud，写回新状态
  - 本机重启时也会先读心跳合并云端期间产生的状态，保证计数连续

Secrets（仓库 Settings → Secrets and variables → Actions）：
  TG_STRING_SESSION / TG_API_ID / TG_API_HASH   已有（cloud_hot_search 在用）
  GITHUB_TOKEN                                  Actions 自动提供（contents: write）
"""
import asyncio, base64, json, os, re, sys, time
from datetime import datetime, timezone

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl import functions

# ---------- 常量 ----------
REPO = "qianjia2021-coder/gmgn-tg-monitor"
HEARTBEAT_PATH = "blackhat_heartbeat.json"
GH_API = "https://api.github.com"

SOURCE = "gmgn100x"
TARGET_CHAT_ID = -5499948080
THRESHOLD = 3
HEARTBEAT_FRESH_SECONDS = 4 * 60   # 本机 4 分钟内有心跳 = 在线
MAX_POLL_ROUNDS = 6                # 云端单次运行最多拉几轮消息（防止 Actions 超时）
POLL_INTERVAL = 3                  # 轮次之间间隔秒

# ---------- 解析（与 handoff/relay.py 完全一致） ----------
TOKEN_RE = re.compile(r'gmgn\.ai/(?:[a-z0-9]+/)?token/([^\s/?#]+)')
EVM_RE = re.compile(r'\b0x[a-fA-F0-9]{40}\b')
BASE58_RE = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')


def log(*a):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def norm(tok):
    return tok[len("10Xboost_"):] if tok.startswith("10Xboost_") else tok


def valid(addr):
    if re.fullmatch(r"0x[a-fA-F0-9]{40}", addr):
        return "EVM"
    if BASE58_RE.fullmatch(addr):
        return "SOL"
    return None


def parse_msg(text, entities):
    contracts = set()
    for e in (entities or []):
        u = getattr(e, "url", None)
        if not u:
            continue
        for m in TOKEN_RE.finditer(u):
            contracts.add(norm(m.group(1)))
    for m in EVM_RE.finditer(text or ""):
        contracts.add(m.group(0))
    contracts = {c for c in contracts if valid(c)}
    if not contracts:
        return None
    lines = (text or "").split("\n")
    head = lines[0] if lines else ""
    body = lines[1] if len(lines) > 1 else ""
    pusher = head.split("]")[0].lstrip("[") if "]" in head else "?"
    chain = head.split(" - ")[1].strip() if " - " in head else "?"
    side = "Buy" if "Buy" in body else ("Sell" if "Sell" in body else "?")
    amt = ""
    ma = re.search(r"(?:Buy|Sell)\s+([\d.]+[KMB]?)\s+(\S+)\s+([\d.]+[KMB]?)", body)
    if ma:
        amt = f"{ma.group(1)} {ma.group(2)} MC {ma.group(3)}"
    tok = ""
    mt = re.search(r"\[\s*([^\]]*?)\s*\]", body)
    if mt:
        tok = mt.group(1)
    return {"contracts": sorted(contracts), "pusher": pusher, "chain": chain,
            "side": side, "amt": amt, "token": tok}


def hesc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_text(addr, chain, count, h):
    rows = h["rows"]
    L = ["📡 Blackhat SB GMGN · 三次推送确认", ""]
    L.append(f"合约: <code>{addr}</code>")
    L.append(f"链: {hesc(chain)}")
    if h.get("token"):
        L.append(f"代币: {hesc(h['token'])}")
    L.append(f"推送次数: {count}")
    L.append(f"推送来源: {len(set(r['pusher'] for r in rows))} 个")
    L.append(f"首次: {rows[0]['ts']}")
    L.append(f"末次: {rows[-1]['ts']}")
    L.append("")
    L.append("📊 推送明细")
    for r in rows:
        L.append(f"· {r['ts']} {hesc(r['side'])} {hesc(r['amt'])} | {hesc(r['pusher'])}")
    L.append("")
    L.append(f"🔗 GMGN: https://gmgn.ai/sol/token/{addr}")
    L.append(f"🔗 Solscan: https://solscan.io/token/{addr}")
    return "\n".join(L)


# ---------- GitHub 心跳读写 ----------

def gh_headers():
    tok = (os.environ.get("GH_TOKEN") or os.environ.get("GMGN_GH_TOKEN")
           or os.environ["GITHUB_TOKEN"])
    return {"Authorization": "token " + tok,
            "Accept": "application/vnd.github+json",
            "User-Agent": "blackhat-relay-cloud"}


def read_heartbeat():
    r = requests.get(f"{GH_API}/repos/{REPO}/contents/{HEARTBEAT_PATH}",
                     headers=gh_headers(), timeout=20)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    j = r.json()
    data = json.loads(base64.b64decode(j["content"]).decode("utf-8"))
    return data, j["sha"]


def write_heartbeat(data, sha, msg):
    payload = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    body = {"message": msg,
            "content": base64.b64encode(payload).decode(),
            "branch": "main"}
    if sha:
        body["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{REPO}/contents/{HEARTBEAT_PATH}",
                     headers=gh_headers(), json=body, timeout=30)
    r.raise_for_status()


def heartbeat_is_local_fresh(hb):
    """本机在线 = 心跳存在 + identity=local + ts 在 HEARTBEAT_FRESH_SECONDS 内"""
    if not hb:
        return False, "no heartbeat"
    if hb.get("identity") != "local":
        return False, f"identity={hb.get('identity')}"
    ts_str = hb.get("ts", "")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception as e:
        return False, f"bad ts: {e}"
    age = time.time() - ts
    if age < HEARTBEAT_FRESH_SECONDS:
        return True, f"local heartbeat age {int(age)}s"
    return False, f"stale age {int(age)}s"


# ---------- 主流程 ----------

async def run():
    hb, hb_sha = read_heartbeat()
    online, reason = heartbeat_is_local_fresh(hb)
    log(f"heartbeat check: online={online} ({reason})")
    if online:
        log("本机在线，云端本轮跳过")
        return 0

    # 从心跳恢复状态
    state = (hb or {}).get("state") or {}
    state.setdefault("counts", {})
    state.setdefault("pushed", {})
    state.setdefault("hist", {})
    state.setdefault("last_id", 0)
    log(f"recovered: last_id={state['last_id']}, "
        f"tracked={len(state['counts'])}, pushed={len(state['pushed'])}")

    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    string_session = os.environ["TG_STRING_SESSION"]
    client = TelegramClient(StringSession(string_session), api_id, api_hash)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            log("FATAL: StringSession 未授权")
            return 1
        me = await client.get_me()
        log(f"logged in as {me.id} @{me.username}")

        src = await client.get_entity(SOURCE)
        tgt = await client.get_entity(TARGET_CHAT_ID)
        log(f"source: {getattr(src,'title','?')} | target: {getattr(tgt,'title','?')}")

        # 跨机去重：读目标群最近 500 条已转发合约
        sent_cache = set()
        try:
            h = await asyncio.wait_for(client(functions.messages.GetHistoryRequest(
                peer=TARGET_CHAT_ID, offset_id=0, offset_date=0, add_offset=0,
                limit=500, max_id=0, min_id=0, hash=0)), timeout=45)
            for m in h.messages:
                t = m.message or ""
                if t.startswith("📡 Blackhat SB GMGN"):
                    for mm in re.finditer(r'^合约:\s*(\S+)', t, re.M):
                        sent_cache.add(mm.group(1))
        except Exception as e:
            log("读目标群历史失败（去重降级）:", type(e).__name__, str(e)[:100])
        log(f"目标群已转发合约（去重基线）: {len(sent_cache)}")

        # 如果没有 last_id（首次云端运行），从最新消息开始
        if not state["last_id"]:
            h = await asyncio.wait_for(client(functions.messages.GetHistoryRequest(
                peer=src, offset_id=0, offset_date=0, add_offset=0,
                limit=1, max_id=0, min_id=0, hash=0)), timeout=30)
            state["last_id"] = h.messages[0].id if h.messages else 0
            log(f"baseline last_id = {state['last_id']}")

        total_pushed = 0
        for round_no in range(1, MAX_POLL_ROUNDS + 1):
            # 拉 min_id=last_id 的新消息
            fresh = []
            h = await asyncio.wait_for(client(functions.messages.GetHistoryRequest(
                peer=src, offset_id=0, offset_date=0, add_offset=0,
                limit=100, max_id=0, min_id=state["last_id"], hash=0)), timeout=45)
            fresh.extend([m for m in h.messages if m.id > state["last_id"]])
            # 攒满 100 条则向前补齐
            if len(fresh) >= 100:
                oldest = min(m.id for m in fresh)
                while True:
                    h2 = await asyncio.wait_for(client(functions.messages.GetHistoryRequest(
                        peer=src, offset_id=oldest, offset_date=0, add_offset=0,
                        limit=100, max_id=0, min_id=state["last_id"], hash=0)), timeout=45)
                    extra = [m for m in h2.messages if state["last_id"] < m.id < oldest]
                    if not extra:
                        break
                    fresh.extend(extra)
                    oldest = min(m.id for m in extra)
                    if len(extra) < 100:
                        break

            if not fresh:
                log(f"[round {round_no}] 无新消息")
                break

            batch = sorted(fresh, key=lambda m: m.id)
            log(f"[round {round_no}] 拉到 {len(batch)} 条新消息, "
                f"last_id {state['last_id']} -> {batch[-1].id}")

            for m in batch:
                state["last_id"] = max(state["last_id"], m.id)
                if not m.message:
                    continue
                info = parse_msg(m.message, m.entities)
                if not info:
                    continue
                ts = m.date.strftime("%m-%d %H:%M:%S")
                for addr in info["contracts"]:
                    c = state["counts"].get(addr, 0) + 1
                    state["counts"][addr] = c
                    hst = state["hist"].setdefault(
                        addr, {"rows": [], "token": "", "chain": info["chain"]})
                    hst["rows"].append({"ts": ts, "side": info["side"],
                                        "amt": info["amt"], "pusher": info["pusher"]})
                    hst["rows"] = hst["rows"][-40:]
                    if info["token"] and not hst["token"]:
                        hst["token"] = info["token"]
                    hst["chain"] = info["chain"]

                    already = state["pushed"].get(addr)
                    hit = (not already and c >= THRESHOLD)
                    if hit and addr in sent_cache:
                        state["pushed"].setdefault(addr, c)
                        log(f"skip（已在目标群转发过）: {addr}")
                        hit = False
                    if hit:
                        try:
                            await client.send_message(
                                TARGET_CHAT_ID,
                                build_text(addr, info["chain"], c, hst),
                                parse_mode="html", link_preview=False)
                            state["pushed"][addr] = c
                            sent_cache.add(addr)
                            total_pushed += 1
                            log(f"✅ PUSHED | {addr} | count={c} | "
                                f"{info['pusher']} {info['side']} {info['amt']}")
                        except Exception as e:
                            log("SEND FAIL:", type(e).__name__, str(e)[:120])
                            state["counts"][addr] = c - 1
                    elif c <= THRESHOLD:
                        log(f"count {addr} = {c} | {info['pusher']} {info['side']} {info['amt']}")

            # 每轮之间短暂等待，看是否还有新消息涌入
            if round_no < MAX_POLL_ROUNDS:
                await asyncio.sleep(POLL_INTERVAL)

        log(f"本轮处理完成: pushed={total_pushed}, last_id={state['last_id']}, "
            f"tracked={len(state['counts'])}, pushed_total={len(state['pushed'])}")

    finally:
        await client.disconnect()

    # 写回心跳（identity=cloud）
    new_hb = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "identity": "cloud",
        "state": state,
    }
    try:
        write_heartbeat(new_hb, hb_sha,
                        f"cloud relay: pushed {total_pushed}, last_id={state['last_id']} [skip ci]")
        log("heartbeat written (identity=cloud)")
    except Exception as e:
        log("写心跳失败:", type(e).__name__, str(e)[:200])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
