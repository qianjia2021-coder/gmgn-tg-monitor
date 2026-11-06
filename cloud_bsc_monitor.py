# -*- coding: utf-8 -*-
"""BH BSC SINGLEBUYS 云端兜底监控（独立凭据，不与本机共用 auth_key）。

- 仅当本机心跳过期（>3 分钟）时才推送，避免双端同时推
- 独立 secrets: BSC_TG_API_ID / BSC_TG_API_HASH / BSC_TG_STRING_SESSION
- 独立状态文件: bsc_singlebuys_seen.json / bsc_singlebuys_cursor.json
- 24 小时扫描窗口，覆盖本机关机间隙
"""
import asyncio, base64, hashlib, json, os, re, sys, time
import urllib.error, urllib.request
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

API_ID = int(os.environ["BSC_TG_API_ID"])
API_HASH = os.environ["BSC_TG_API_HASH"]
STRING = os.environ["BSC_TG_STRING_SESSION"]

GH_REPO = "qianjia2021-coder/gmgn-tg-monitor"
GH_API = "https://api.github.com"

CHAT_USERNAME = "empirebscvolume"
TARGET_INVITE_HASH = "0NCSpGbGf_MyNWU1"

GH_SEEN_PATH = "bsc_singlebuys_seen.json"
GH_CURSOR_PATH = "bsc_singlebuys_cursor.json"
GH_HEARTBEAT_PATH = "bsc_singlebuys_heartbeat.txt"

SCAN_WINDOW_MIN = 24 * 60
SCAN_LIMIT = 1000
HEARTBEAT_STALE_SEC = 180   # 本机心跳超过 3 分钟视为离线，云端接管

CA_RE = re.compile(r"CA:\s*(0x[a-fA-F0-9]{40})")
EVM_RE = re.compile(r"\b(0x[a-fA-F0-9]{40})\b")


def log(msg):
    print("[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def extract(text):
    addrs = CA_RE.findall(text or "")
    return addrs if addrs else EVM_RE.findall(text or "")


def gh_headers():
    return {"Authorization": "token " + os.environ["GITHUB_TOKEN"],
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
        return None, None
    except Exception as e:
        log("读取 {} 失败: {}".format(path, e))
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
                log("写入 {} OK".format(path))
                return True
        except urllib.error.HTTPError as e:
            if e.code == 409 and attempt < 2:
                _, sha = gh_read(path)
                body["sha"] = sha
                time.sleep(2)
                continue
            log("写入 {} 失败: HTTP {}".format(path, e.code))
            return False
        except Exception as e:
            log("写入 {} 异常: {}".format(path, e))
            time.sleep(2)
    return False


def local_is_online():
    """检查本机心跳是否新鲜。"""
    raw, _ = gh_read(GH_HEARTBEAT_PATH)
    if not raw:
        return False
    try:
        # 格式: "bsc_singlebuys hb 2026-11-06T12:00:00Z"
        ts = raw.strip().rsplit(" ", 1)[-1]
        hb_time = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - hb_time).total_seconds()
        log("本机心跳年龄: {:.0f}s".format(age))
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
    # 先检查本机是否在线，在线就不接管
    if local_is_online():
        log("本机在线，本轮云端跳过（避免双端重复推送）")
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

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=SCAN_WINDOW_MIN)
        bj = timezone(timedelta(hours=8))
        pushed = 0
        scanned = 0
        max_id = cursor

        async for m in client.iter_messages(chat, limit=SCAN_LIMIT):
            scanned += 1
            if m.id <= cursor:
                continue
            if m.date is None or m.date < cutoff:
                break
            for addr in extract(m.message or ""):
                if addr in seen:
                    continue
                seen.add(addr)
                if m.id > max_id:
                    max_id = m.id
                if target is None:
                    continue
                now_bj = datetime.now(bj).strftime("%Y-%m-%d %H:%M:%S")
                src_bj = m.date.astimezone(bj).strftime("%Y-%m-%d %H:%M:%S")
                text = ("BH BSC SINGLEBUYS 新合约\n"
                        "出现时间: {}\n"
                        "推送时间: {}\n"
                        "\n"
                        "{}").format(src_bj, now_bj, addr)
                try:
                    await client.send_message(target, text, parse_mode="html")
                    log("  已推送: {}".format(addr))
                    pushed += 1
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
