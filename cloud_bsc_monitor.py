# -*- coding: utf-8 -*-
"""BH BSC SINGLEBUYS 云端兜底监控（独立凭据，不与本机共用 auth_key）。

修复记录见 CHANGELOG.md。
"""
import asyncio, base64, json, os, re, sys, time
import urllib.error, urllib.request
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.errors import FloodWaitError

API_ID = int(os.environ.get("BSC_TG_API_ID", "0"))
API_HASH = os.environ.get("BSC_TG_API_HASH", "")
STRING = os.environ.get("BSC_TG_STRING_SESSION", "")

# P0-10：GITHUB_TOKEN 用 .get()，缺失时明确报错
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

GH_REPO = "qianjia2021-coder/gmgn-tg-monitor"
GH_API = "https://api.github.com"

CHAT_USERNAME = "empirebscvolume"
TARGET_INVITE_HASH = "0NCSpGbGf_MyNWU1"

GH_SEEN_PATH = "bsc_singlebuys_seen.json"
GH_CURSOR_PATH = "bsc_singlebuys_cursor.json"
GH_HEARTBEAT_PATH = "bsc_singlebuys_heartbeat.txt"

DBOTX_KEY = "fgfz61yx7zv305l9r9q5prjrp521pgj2"
DBOTX_URL = "https://api-bot-v1.dbotx.com/simulator/sim_swap_order"

SCAN_WINDOW_MIN = 24 * 60
SCAN_LIMIT = 1000
# P1-6：心跳阈值 300s（原 180s 太紧，同仓库已验证安全值）
HEARTBEAT_STALE_SEC = 300

CA_RE = re.compile(r"CA:\s*(0x[a-fA-F0-9]{40})")
EVM_RE = re.compile(r"\b(0x[a-fA-F0-9]{40})\b")


def log(msg):
    print("[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def norm_addr(a):
    return a.lower()


def extract(text):
    addrs = CA_RE.findall(text or "")
    if addrs:
        return [norm_addr(a) for a in addrs]
    return [norm_addr(a) for a in EVM_RE.findall(text or "")]


def gh_headers():
    return {"Authorization": "token " + GITHUB_TOKEN,
            "Accept": "application/vnd.github+json",
            "User-Agent": "bsc-cloud-monitor"}


def gh_read(path):
    try:
        req = urllib.request.Request(
            "{}/repos/{}/contents/{}".format(GH_API, GH_REPO, path), headers=gh_headers())
        with urllib.request.urlopen(req, timeout=25) as r:
            j = json.loads(r.read().decode())
        return base64.b64decode(j["content"]).decode(), j.get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        log("读取 {} 失败: HTTP {}".format(path, e.code))
        return None, None
    except Exception as e:
        log("读取 {} 异常: {}".format(path, e))
        return None, None


def gh_write(path, content, sha, msg):
    body = {"message": msg,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": "main"}
    if sha:
        body["sha"] = sha
    for attempt in range(3):
        req = urllib.request.Request(
            "{}/repos/{}/contents/{}".format(GH_API, GH_REPO, path),
            data=json.dumps(body).encode(), method="PUT",
            headers={**gh_headers(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                log("写入 {} OK (HTTP {})".format(path, r.status))
                return True
        except urllib.error.HTTPError as e:
            if e.code == 409 and attempt < 2:
                _, sha = gh_read(path)
                body["sha"] = sha
                time.sleep(2)
                continue
            if e.code == 403:
                log("!! 写入 {} 被拒: HTTP 403（workflow permissions 不足）".format(path))
            else:
                log("写入 {} 失败: HTTP {}".format(path, e.code))
            return False
        except Exception as e:
            log("写入 {} 异常: {}".format(path, e))
            time.sleep(2)
    return False


def local_is_online():
    raw, _ = gh_read(GH_HEARTBEAT_PATH)
    if not raw:
        return False
    try:
        ts = raw.strip().rsplit(" ", 1)[-1]
        hb_time = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - hb_time).total_seconds()
        log("本机心跳年龄: {:.0f}s (阈值 {}s)".format(age, HEARTBEAT_STALE_SEC))
        return age < HEARTBEAT_STALE_SEC
    except Exception as e:
        log("心跳解析失败: {}".format(e))
        return False


async def resolve_target(client):
    try:
        res = await client(CheckChatInviteRequest(hash=TARGET_INVITE_HASH))
        chat = getattr(res, "chat", None)
        if chat:
            return chat
    except Exception:
        pass
    try:
        upd = await client(ImportChatInviteRequest(hash=TARGET_INVITE_HASH))
        for c in getattr(upd, "chats", None) or []:
            return c
    except Exception as e:
        log("ImportChatInvite 失败: {}".format(e))
    return None


def dbotx_buy(addr):
    """云端推送后同样在 dbotx 模拟器买入 1 BNB，止盈 50%。"""
    body = {
        "chain": "bsc",
        "pair": addr,
        "type": "buy",
        "amountOrPercent": 1.0,
        "stopEarnPercent": 0.5,
        "stopLossPercent": None,
        "gasFeeDelta": 5,
        "maxFeePerGas": 100,
        "slippage": 0.1,
    }
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                DBOTX_URL,
                data=json.dumps(body).encode(),
                method="POST",
                headers={
                    "x-api-key": DBOTX_KEY,
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                })
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read().decode())
            if not res.get("err"):
                log("  dbotx 买入成功: {} order_id={}".format(addr, res["res"]["id"]))
                return True
            log("  dbotx 返回错误 (第{}/3): {}".format(attempt + 1, res))
        except Exception as e:
            log("  dbotx 买入失败 (第{}/3) {}: {}".format(attempt + 1, addr, e))
        time.sleep(3)
    log("  !! dbotx 三次买入均失败，需手动补单: {}".format(addr))
    return False


async def connect_with_retry():
    last = None
    for attempt in range(4):
        client = None
        try:
            client = TelegramClient(StringSession(STRING), API_ID, API_HASH,
                                    connection_retries=2, retry_delay=3,
                                    timeout=30, request_retries=2)
            await asyncio.wait_for(client.connect(), timeout=60)
            if not await asyncio.wait_for(client.is_user_authorized(), timeout=30):
                await client.disconnect()
                raise RuntimeError("会话未授权")
            return client
        except Exception as e:
            last = e
            log("连接尝试 {} 失败: {}".format(attempt + 1, e))
            if client:
                try: await client.disconnect()
                except: pass
            await asyncio.sleep(5 * (attempt + 1))
    raise RuntimeError("无法连接: {}".format(last))


async def main():
    if not GITHUB_TOKEN:
        log("FATAL: GITHUB_TOKEN 未注入")
        sys.exit(1)

    if local_is_online():
        log("本机在线，本轮云端跳过")
        return

    seen = set()
    raw, sha_seen = gh_read(GH_SEEN_PATH)
    if raw:
        try:
            seen = set(json.loads(raw))
            log("已读取共享 seen: {} 个".format(len(seen)))
        except Exception:
            pass

    cursor = 0
    raw_c, sha_cursor = gh_read(GH_CURSOR_PATH)
    if raw_c:
        try:
            cursor = int(json.loads(raw_c).get("last_msg_id", 0))
        except Exception:
            pass

    client = await connect_with_retry()
    try:
        me = await client.get_me()
        log("云端登录: @{} (id={})".format(getattr(me, "username", ""), me.id))

        chat = await client.get_entity(CHAT_USERNAME)
        log("监听群: {}".format(getattr(chat, "title", "?")))

        target = await resolve_target(client)
        if target:
            log("推送目标: {}".format(getattr(target, "title", "?")))

        # 残留-1：target is None 直接中止本轮，不扫描、不推进游标、不写回
        if target is None:
            log("!! 目标群解析失败，本轮中止，不写游标不写 seen")
            return

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=SCAN_WINDOW_MIN)
        bj = timezone(timedelta(hours=8))
        pushed = 0
        scanned = 0
        max_id = cursor

        async for m in client.iter_messages(chat, limit=SCAN_LIMIT):
            scanned += 1
            # P1-12：遇到已处理的游标直接 break，不 continue
            if m.id <= cursor:
                break
            if m.date is None or m.date < cutoff:
                break
            if m.id > max_id:
                max_id = m.id
            for addr in extract(m.message or ""):
                if addr in seen:
                    continue
                now_bj = datetime.now(bj).strftime("%Y-%m-%d %H:%M:%S")
                src_bj = m.date.astimezone(bj).strftime("%Y-%m-%d %H:%M:%S")
                text = ("BH BSC SINGLEBUYS 新合约\n"
                        "出现时间: {}\n"
                        "推送时间: {}\n"
                        "\n"
                        "<code>{}</code>").format(src_bj, now_bj, addr)
                try:
                    await client.send_message(target, text, parse_mode="html")
                    seen.add(addr)
                    log("  已推送: {}".format(addr))
                    pushed += 1
                    dbotx_buy(addr)
                except FloodWaitError as e:
                    log("  FloodWait {}s，等待后重试".format(e.seconds))
                    await asyncio.sleep(e.seconds + 1)
                    try:
                        await client.send_message(target, text, parse_mode="html")
                        seen.add(addr)
                        pushed += 1
                        dbotx_buy(addr)
                    except Exception as e2:
                        log("  重试仍失败 {}: {}".format(addr, e2))
                except Exception as e:
                    log("  推送失败 {}: {}".format(addr, e))
                await asyncio.sleep(1)

        log("扫描 {} 条，推送 {} 个，seen 总数 {}".format(scanned, pushed, len(seen)))

        gh_write(GH_SEEN_PATH,
                 json.dumps(sorted(seen), ensure_ascii=False, indent=1),
                 sha_seen, "cloud bsc seen [skip ci]")
        if max_id > cursor:
            gh_write(GH_CURSOR_PATH,
                     json.dumps({"last_msg_id": max_id,
                                 "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                                indent=1),
                     sha_cursor, "cloud bsc cursor [skip ci]")
    finally:
        try: await client.disconnect()
        except: pass
    log("done")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        log("FATAL: {}: {}".format(type(e).__name__, str(e)[:300]))
        sys.exit(1)
