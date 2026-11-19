# -*- coding: utf-8 -*-
"""Empire SOL Trending 公共模块。

本模块统一承载三类逻辑：

1. 合约地址的正则、校验与提取。本机监控、云端兜底、基线构建三处共用同一套
   规则，避免各文件规则漂移导致远端去重集与本地去重集口径不一致。
2. GitHub Contents API 的读写封装。该类持有每个路径的 sha 缓存，写入时优先
   复用缓存 sha，冲突后重新读取并重试，从而减少往返请求次数。
3. 心跳文件的时间戳解析，供本机与云端按同一阈值判定在线状态。

本模块不包含任何凭据。调用方负责注入 token 与日志函数。
"""
import base64
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------- 地址识别 ----------
B58 = r"1-9A-HJ-NP-Za-km-z"
CA_RE = re.compile(r"CA:\s*([" + B58 + r"]{32,44})")
TOKEN_URL_RE = re.compile(r"gmgn\.ai/sol/token/(?:10Xboost_)?([" + B58 + r"]{32,44})")
PLAIN_RE = re.compile(r"\b([" + B58 + r"]{32,44})\b")
SOL_NATIVE = "So11111111111111111111111111111111111111112"

try:
    from telethon.tl.types import MessageEntityTextUrl as _TextUrl

    def is_text_url(entity) -> bool:
        """判断消息实体是否为文本链接。"""
        return isinstance(entity, _TextUrl)
except ImportError:
    def is_text_url(entity) -> bool:
        """在无 telethon 环境下按类型名判断消息实体。"""
        return type(entity).__name__ == "MessageEntityTextUrl"


def valid_b58(addr: str) -> bool:
    """校验一个字符串是否为合法的 Solana 合约地址。

    校验条件包含长度区间、禁用字符、Base58 字符集，以及排除 SOL 原生包装
    代币。排除原生包装代币的理由在于该地址并非新上线合约，推送它没有意义。
    """
    if not (32 <= len(addr) <= 44):
        return False
    if addr == SOL_NATIVE:
        return False
    if any(c in addr for c in "0OIl"):
        return False
    return bool(PLAIN_RE.fullmatch(addr))


def extract_addrs(msg) -> list:
    """从消息对象或纯文本中提取合约地址，并按出现顺序去重。

    参数 msg 可以是一段字符串，也可以是带有 message 与 entities 属性的
    Telegram 消息对象。函数先处理正文中的 CA 标记与 gmgn 链接，仅在正文
    未提取到地址时才回退到裸 Base58 候选，最后补充消息实体中的链接地址。
    """
    if isinstance(msg, str):
        text = msg
        entities = []
    else:
        text = getattr(msg, "message", None) or ""
        entities = getattr(msg, "entities", None) or []

    addrs: list = []

    def _append(candidates):
        for candidate in candidates:
            if valid_b58(candidate) and candidate not in addrs:
                addrs.append(candidate)

    _append(CA_RE.findall(text))
    _append(TOKEN_URL_RE.findall(text))
    if not addrs:
        _append(PLAIN_RE.findall(text))
    for entity in entities:
        if is_text_url(entity):
            _append(TOKEN_URL_RE.findall(getattr(entity, "url", "") or ""))
    return addrs


def extract_symbol(text: str) -> str:
    """从消息正文开头提取形如 #SYMBOL 的代币符号。"""
    match = re.match(r"\s*#([A-Za-z0-9_.\-]+)", text or "")
    return match.group(1) if match else ""


# ---------- GitHub Contents ----------
class GitHubContents:
    """封装 GitHub Contents API 的读取与写入，并维护路径到 sha 的缓存。"""

    def __init__(self, repo: str, token: str,
                 api: str = "https://api.github.com",
                 ua: str = "empire-sol-trending",
                 log=None, timeout: int = 25):
        """初始化仓库、令牌、接口根地址、日志函数与超时时间。"""
        self.repo = repo
        self.token = token
        self.api = api.rstrip("/")
        self.ua = ua
        self.log = log if log else (lambda message: None)
        self.timeout = timeout
        self._sha = {}

    def _headers(self) -> dict:
        """构造携带令牌与 User-Agent 的请求头。"""
        return {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "User-Agent": self.ua,
        }

    def _url(self, path: str) -> str:
        """拼接某个仓库文件的 Contents API 地址。"""
        return "{}/repos/{}/contents/{}".format(self.api, self.repo, path)

    def read(self, path: str):
        """读取文件内容，返回 (文本, sha)；文件不存在时返回 (None, None)。"""
        if not self.token:
            return None, None
        try:
            request = urllib.request.Request(self._url(path), headers=self._headers())
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode())
            sha = payload.get("sha")
            if sha:
                self._sha[path] = sha
            return base64.b64decode(payload["content"]).decode(), sha
        except urllib.error.HTTPError as error:
            if error.code == 404:
                self._sha.pop(path, None)
            elif error.code != 404:
                self.log("GitHub 读 {} 失败 HTTP {}".format(path, error.code))
            return None, None
        except Exception as error:
            self.log("GitHub 读 {} 异常: {}".format(path, error))
            return None, None

    def write(self, path: str, content: str, message: str, branch: str = "main") -> bool:
        """写入文件内容，返回是否成功。

        方法优先使用缓存中的 sha。服务端返回 409 冲突时，方法重新读取文件
        取得最新 sha 后重试，最多尝试三次。写入成功后，方法把响应中的新
        sha 记入缓存，使后续写入无需额外发起一次读取请求。
        """
        if not self.token:
            return False
        encoded = base64.b64encode(content.encode()).decode()
        # 冷启动时缓存为空。若文件已存在而 PUT 未带 sha，GitHub 会返回 422。
        # 因此首次写入前主动读取一次，取得现有 sha。
        if path not in self._sha:
            self.read(path)
        for attempt in range(3):
            body = {"message": message, "content": encoded, "branch": branch}
            cached_sha = self._sha.get(path)
            if cached_sha:
                body["sha"] = cached_sha
            request = urllib.request.Request(
                self._url(path),
                data=json.dumps(body).encode(),
                method="PUT",
                headers={**self._headers(), "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode() or "{}")
                    new_sha = (payload.get("content") or {}).get("sha")
                    if new_sha:
                        self._sha[path] = new_sha
                    return response.status in (200, 201)
            except urllib.error.HTTPError as error:
                # 409 表示 sha 过期，422 表示缺少 sha，两种情况都重新读取后再试。
                if error.code in (409, 422) and attempt < 2:
                    self.read(path)
                    continue
                self.log("GitHub 写 {} 失败 HTTP {}".format(path, error.code))
                return False
            except Exception as error:
                self.log("GitHub 写 {} 异常: {}".format(path, error))
                time.sleep(2)
        return False


def heartbeat_age_seconds(raw: str):
    """解析心跳文件内容并返回其年龄秒数；解析失败时返回 None。"""
    if not raw:
        return None
    try:
        stamp = raw.strip().rsplit(" ", 1)[-1]
        moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - moment).total_seconds()
    except Exception:
        return None
