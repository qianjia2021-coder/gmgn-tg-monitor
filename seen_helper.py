# -*- coding: utf-8 -*-
"""
seen 文件与云端同步的 Python 辅助（绕开 PS 5.1 ConvertFrom-Json 的 BOM/单元素数组缺陷）
用法:
  python seen_helper.py read  <local_path>            # stdout 每行一个地址（已去重）
  python seen_helper.py write <local_path>            # 从 stdin 读地址（每行一个），写无 BOM JSON 数组
  python seen_helper.py pull  <local_path> <chain>    # 拉云端 -> 与本地并集 -> 写本地；stdout 摘要
  python seen_helper.py push  <local_path> <chain>    # 本地 -> 与云端最新并集 -> 写本地 -> 上传；stdout 摘要
"""
import base64
import json
import os
import subprocess
import sys
import urllib.request

REPO = "qianjia2021-coder/gmgn-tg-monitor"
EVM_CHAINS = ("bsc", "base", "eth", "arbitrum", "robinhood", "hyperevm", "arc", "stable")


def get_token():
    out = subprocess.run(
        ["reg", "query", r"HKCU\Environment", "/v", "GMGN_GH_TOKEN"],
        capture_output=True, text=True).stdout
    if "REG_SZ" not in out:
        return ""
    return out.split("REG_SZ")[-1].strip()


def read_addrs(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, list):
            return sorted(set(str(x).strip() for x in d if isinstance(x, str) and x.strip()))
    except Exception:
        pass
    return []


def write_addrs(path, addrs):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(sorted(set(addrs)), ensure_ascii=False))
    os.replace(tmp, path)


def _api(path, method="GET", body=None, tok=None):
    if not tok:
        tok = get_token()
    headers = {"Authorization": f"Bearer {tok}",
               "Accept": "application/vnd.github+json",
               "User-Agent": "gmgn-local"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}{path}",
                                 data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def fetch_cloud():
    cur = _api("/contents/seen.json")
    cloud = json.loads(base64.b64decode(cur["content"]))
    if isinstance(cloud, list):
        cloud = sorted(set(str(x).strip() for x in cloud if isinstance(x, str) and x.strip()))
    else:
        cloud = []
    return cur, cloud


def filter_chain(addrs, chain=None):
    # 跨链保留：EVM 地址(0x) 与 SOL Pump 合约(pump 尾缀) 共存于同一 seen，
    # 避免单一链任务回写云端时把其它链的已发记录覆盖丢失
    return sorted(a for a in addrs if a.startswith("0x") or a.endswith("pump"))


def pull(local, chain):
    cur, cloud = fetch_cloud()
    local_addrs = read_addrs(local)
    merged = filter_chain(set(cloud) | set(local_addrs), chain)
    write_addrs(local, merged)
    print(f"PULL_OK cloud={len(cloud)} local={len(local_addrs)} merged={len(merged)}")


def push(local, chain):
    cur, cloud = fetch_cloud()
    local_addrs = read_addrs(local)
    merged = filter_chain(set(cloud) | set(local_addrs), chain)
    write_addrs(local, merged)
    payload = {
        "message": "sync seen from local",
        "content": base64.b64encode(json.dumps(merged, ensure_ascii=False).encode()).decode(),
        "sha": cur["sha"],
        "branch": "main",
    }
    _api("/contents/seen.json", method="PUT", body=payload)
    print(f"PUSH_OK local={len(local_addrs)} cloud={len(cloud)} merged={len(merged)}")


def main():
    cmd = sys.argv[1]
    if cmd == "read":
        for a in read_addrs(sys.argv[2]):
            print(a)
    elif cmd == "write":
        addrs = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
        write_addrs(sys.argv[2], addrs)
    elif cmd == "pull":
        pull(sys.argv[2], sys.argv[3])
    elif cmd == "push":
        push(sys.argv[2], sys.argv[3])
    else:
        print(f"unknown cmd: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
