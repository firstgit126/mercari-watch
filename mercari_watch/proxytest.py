"""代理体检。

买代理之前最怕两件事：
  1. 买完发现根本连不上煤炉（很多"日区代理"其实出口不在日本，或者被 Cloudflare 拦）；
  2. 端口通但账号密码错。

所以这里做四件事：解析代理地址 → TCP 握手 → 查出口 IP 归属国 → 真的用这个代理拉一次煤炉搜索页。
任何一步不过，就明确告诉你卡在哪，不用猜。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.parse

from .config import Network
from .fetcher import SEARCH_WEB, parse_items_from_html
from .http import HttpClient, HttpError

# 用来查「出口 IP 和它在哪个国家」的接口，按顺序尝试
TRACE_URLS = [
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://ipinfo.io/json",
    "https://api.ipify.org?format=json",
]

SUPPORTED_SCHEMES = {"http", "https"}


def mask_proxy(proxy: str) -> str:
    """打日志时把密码遮掉。"""
    parsed = urllib.parse.urlparse(proxy)
    auth = f"{parsed.username}:***@" if parsed.username else ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{auth}{parsed.hostname}{port}"


def parse_proxy(proxy: str) -> dict:
    parsed = urllib.parse.urlparse(proxy.strip())
    if not parsed.scheme:
        raise ValueError("代理地址要带协议头，例如 http://user:pass@1.2.3.4:8080")
    if parsed.scheme not in SUPPORTED_SCHEMES:
        raise ValueError(
            f"暂不支持 {parsed.scheme}:// 代理。"
            "请用 http:// 或 https://（大多数代理服务商都提供 HTTP 入口，"
            "SOCKS5 需要先转成 HTTP 入口）"
        )
    if not parsed.hostname:
        raise ValueError("代理地址里没有主机名")
    return {
        "host": parsed.hostname,
        "port": parsed.port or (443 if parsed.scheme == "https" else 80),
        "username": parsed.username or "",
        "has_password": bool(parsed.password),
        "scheme": parsed.scheme,
    }


def _exit_info(client: HttpClient) -> tuple[str, str]:
    """返回 (出口 IP, 国家代码)。"""
    for url in TRACE_URLS:
        try:
            resp = client.request("GET", url, accept="application/json, text/plain, */*")
        except HttpError:
            continue
        text = resp.text.strip()
        if "cdn-cgi/trace" in url:
            data = {}
            for line in text.splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    data[key.strip()] = value.strip()
            if data.get("ip"):
                return data["ip"], data.get("loc", "")
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            ip = data.get("ip") or data.get("query") or ""
            country = data.get("country") or data.get("country_code") or ""
            if ip:
                return ip, country
    return "", ""


def check(network: Network, keyword: str = "Nintendo Switch 有機EL") -> dict:
    """跑完整套代理体检，返回结构化的结果。"""
    result: dict = {
        "proxy": mask_proxy(network.proxy),
        "parsed": {},
        "tcp_ok": False,
        "tcp_ms": 0,
        "exit_ip": "",
        "country": "",
        "http_status": 0,
        "bytes": 0,
        "items": 0,
        "elapsed": 0.0,
        "error": "",
        "stage": "parse",
    }

    try:
        parsed = parse_proxy(network.proxy)
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    result["parsed"] = parsed
    result["stage"] = "tcp"

    try:
        started = time.time()
        with socket.create_connection((parsed["host"], parsed["port"]), timeout=8):
            result["tcp_ms"] = int((time.time() - started) * 1000)
        result["tcp_ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"代理端口连不上：{type(exc).__name__}: {exc}"
        return result

    client = HttpClient(
        proxy=network.proxy,
        timeout=max(10, network.timeout),
        retries=0,
        user_agent=network.user_agent,
    )

    result["stage"] = "exit"
    result["exit_ip"], result["country"] = _exit_info(client)

    # 关键一步：真的用这个代理拉一次煤炉
    result["stage"] = "mercari"
    params = urllib.parse.urlencode(
        {"keyword": keyword, "status": "on_sale", "sort": "created_time", "order": "desc"}
    )
    started = time.time()
    try:
        resp = client.request(
            "GET", f"{SEARCH_WEB}?{params}", headers={"Referer": "https://jp.mercari.com/"}
        )
        result["elapsed"] = time.time() - started
        result["http_status"] = resp.status
        result["bytes"] = len(resp.body)
        if resp.status == 200:
            result["items"] = len(parse_items_from_html(resp.text))
        if resp.status in (403, 429):
            result["error"] = f"煤炉返回 HTTP {resp.status}（出口 IP 可能被风控或地区限制）"
    except HttpError as exc:
        result["error"] = f"通过代理访问煤炉失败：{exc}"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result


def verdict(result: dict) -> tuple[str, list[str]]:
    """把结果翻译成人话：(结论级别, 说明行)。"""
    if result["stage"] == "parse" and result["error"]:
        return "fail", [result["error"]]

    lines: list[str] = []
    if not result["tcp_ok"]:
        return "fail", [
            result["error"],
            "检查代理地址/端口是否写对，以及代理服务是否要求 IP 白名单。",
        ]

    lines.append(
        f"代理端口 {result['parsed']['host']}:{result['parsed']['port']} 可连接（{result['tcp_ms']}ms）"
    )
    if result["parsed"]["username"]:
        lines.append("代理需要账号密码：已带上（如果报 407，就是账号/密码或白名单不对）")

    if result["exit_ip"]:
        lines.append(f"出口 IP：{result['exit_ip']}　地区：{result['country'] or '未知'}")
        if result["country"] and result["country"].upper() not in {"JP", "US", "SG", "HK", "TW", "KR"}:
            lines.append(
                f"⚠️ 出口在 {result['country']}，不在日本/亚太。煤炉对部分地区有限制，"
                "能连上也可能随时被拦，建议换日本节点。"
            )
    else:
        lines.append("没查到出口 IP（查询接口可能被代理挡了），但不影响下面继续判断")

    if result["http_status"]:
        lines.append(
            f"煤炉搜索页：HTTP {result['http_status']}，{result['bytes']:,} 字节，"
            f"{result['elapsed']:.1f}s，解析出 {result['items']} 个商品"
        )

    if result["error"] and result["stage"] == "mercari":
        if result["items"] > 0:
            lines.append(f"注意：{result['error']}（但页面已经拿到商品，可先用）")
            return "warn", lines
        return "fail", lines + [result["error"], "这个代理连不上煤炉，换了它。"]

    if result["items"] > 0:
        return "ok", lines
    if result["http_status"] == 200:
        return "warn", lines + [
            "页面拿到了但没解析出商品，可能是煤炉返回了空结果或页面结构变了；",
            "先用一个确定有货的关键字（比如 ポケモンカード）再试一次。",
        ]
    return "fail", lines + ["没能从煤炉拿到正常响应，这个代理不适合。"]
