"""网络探测：不用 VPN 到底能不能直连煤炉？

沙箱/开发环境里测不出真实网络，所以这个脚本是给你在自己的网络里跑的。
它按「DNS 是否被污染 → 真实 IP 是多少 → 拿真实 IP 直连能不能过」三层剥开，
最后直接给结论。

用法：
    python tools/probe_network.py
    python tools/probe_network.py --keyword "Nintendo Switch 有機EL"
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request

HOSTS = ["jp.mercari.com", "api.mercari.jp"]
BLACKHOLE_PREFIXES = ("199.59.150.", "0.0.0.0", "127.0.0.1")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 国内可达的 DoH JSON 接口（Cloudflare 只作为对照，通常被墙）
DOH_PROVIDERS = [
    ("阿里 DNS", "https://223.5.5.5/resolve"),
    ("腾讯 DNS", "https://doh.pub/dns-query"),
    ("Cloudflare", "https://1.1.1.1/dns-query"),
]


def _section(title: str) -> None:
    print(f"\n{'=' * 62}\n {title}\n{'=' * 62}")


def resolve_system(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return sorted({info[4][0] for info in infos})
    except socket.gaierror as exc:
        print(f"  系统解析失败：{exc}")
        return []


def resolve_doh(host: str, endpoint: str) -> list[str]:
    url = f"{endpoint}?name={urllib.parse.quote(host)}&type=A"
    req = urllib.request.Request(
        url, headers={"Accept": "application/dns-json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    answers = data.get("Answer") or []
    return [a["data"] for a in answers if a.get("type") == 1]


def is_blackhole(ips: list[str]) -> bool:
    return any(ip.startswith(BLACKHOLE_PREFIXES) for ip in ips)


def tcp_tls_probe(ip: str, host: str, timeout: float = 8.0) -> tuple[bool, str]:
    """拿固定 IP 建 TLS 连接，SNI 用真实域名（这是绕污染直连的关键）。"""
    context = ssl.create_default_context()
    try:
        started = time.time()
        with socket.create_connection((ip, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                ms = int((time.time() - started) * 1000)
                return True, f"TLS {tls.version()} / {ms}ms"
    except ssl.SSLError as exc:
        return False, f"TLS 失败：{exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def http_get_via_ip(ip: str, host: str, path: str, timeout: float = 15.0):
    """用固定 IP + 正确 SNI 手写一个 HTTPS GET，返回 (状态码, 头, 正文)。"""
    context = ssl.create_default_context()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"User-Agent: {USER_AGENT}\r\n"
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8\r\n"
        "Accept-Language: ja-JP,ja;q=0.9,zh-CN;q=0.8\r\n"
        "Accept-Encoding: identity\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")

    with socket.create_connection((ip, 443), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            tls.sendall(request)
            chunks = []
            total = 0
            while total < 3_000_000:
                block = tls.recv(65536)
                if not block:
                    break
                chunks.append(block)
                total += len(block)
    payload = b"".join(chunks)
    head, _, body = payload.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1", "replace").split("\r\n")
    status = 0
    if lines and lines[0].startswith("HTTP/"):
        try:
            status = int(lines[0].split()[1])
        except (IndexError, ValueError):
            status = 0
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    return status, headers, body


def classify(body: bytes, headers: dict) -> str:
    text = body.decode("utf-8", "replace")
    if headers.get("cf-mitigated") or "Just a moment" in text or "cf-challenge" in text:
        return "被 Cloudflare 人机校验拦住"
    if "アクセスが集中" in text or "access denied" in text.lower():
        return "被拒绝访问（地区/风控拦截）"
    if '"itemId"' in text or "itemName" in text or "/item/m" in text:
        return "返回了正常页面（含商品数据）"
    return "返回了页面，但没看到商品数据"


def main() -> int:
    parser = argparse.ArgumentParser(description="探测不用 VPN 能否直连煤炉")
    parser.add_argument("--keyword", default="Nintendo Switch 有機EL")
    args = parser.parse_args()

    print("煤炉直连可行性探测")
    print(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")

    real_ips: dict[str, list[str]] = {}
    polluted: dict[str, bool] = {}

    for host in HOSTS:
        _section(f"第 1 层：{host} 的 DNS")
        system_ips = resolve_system(host)
        if system_ips:
            bad = is_blackhole(system_ips)
            polluted[host] = bad
            flag = "  ← 疑似污染/黑洞地址" if bad else ""
            print(f"  系统 DNS   : {', '.join(system_ips)}{flag}")
        else:
            polluted[host] = True

        for name, endpoint in DOH_PROVIDERS:
            try:
                ips = resolve_doh(host, endpoint)
                if ips:
                    print(f"  {name:<11}: {', '.join(ips)}")
                    for ip in ips:
                        if ip not in real_ips.setdefault(host, []):
                            real_ips[host].append(ip)
                else:
                    print(f"  {name:<11}: 无结果")
            except Exception as exc:  # noqa: BLE001
                print(f"  {name:<11}: 请求失败（{type(exc).__name__}）")

    if not real_ips:
        print("\n结论：所有 DoH 都拿不到真实 IP，这台机器的 DNS 出站被完全阻断。")
        print("      只能用「抓取放到海外」的方案（GitHub Actions / VPS）。")
        return 1

    for host in HOSTS:
        ips = real_ips.get(host) or []
        if not ips:
            continue
        _section(f"第 2 层：{host} 真实 IP 的直连测试")
        reachable = []
        for ip in ips[:4]:
            ok, detail = tcp_tls_probe(ip, host)
            print(f"  {'[通]' if ok else '[断]'} {ip:<16} {detail}")
            if ok:
                reachable.append(ip)
        if not reachable:
            print("\n  TLS 全部失败 → 出口 IP 到煤炉被拦，直连无解。")

    # 第 3 层：真拉一次搜索页
    _section("第 3 层：用真实 IP 拉一次搜索结果页")
    host = "jp.mercari.com"
    ips = real_ips.get(host) or []
    path = "/search?" + urllib.parse.urlencode(
        {"keyword": args.keyword, "status": "on_sale", "sort": "created_time", "order": "desc"}
    )
    http_ok = False
    for ip in ips[:4]:
        try:
            status, headers, body = http_get_via_ip(ip, host, path)
        except Exception as exc:  # noqa: BLE001
            print(f"  [断] {ip:<16} {type(exc).__name__}: {exc}")
            continue
        verdict = classify(body, headers)
        print(f"  [HTTP {status}] {ip:<16} {len(body):>8} 字节  → {verdict}")
        if status == 200 and "含商品数据" in verdict:
            http_ok = True
            try:
                sys.path.insert(0, ".")
                from mercari_watch.fetcher import parse_items_from_html

                items = parse_items_from_html(body.decode("utf-8", "replace"))
                print(f"         解析出 {len(items)} 个商品，示例：")
                for item in items[:3]:
                    price = f"¥{item.price:,}" if item.price else "价格未知"
                    print(f"           {item.id}  {price}  {item.name[:36]}")
                if items:
                    http_ok = True
            except Exception as exc:  # noqa: BLE001
                print(f"         解析器调用失败：{exc}")

    _section("结论")
    if http_ok:
        print("  ✅ 直连可用！不需要 VPN。")
        print("     真实 IP 是能连通的，问题只出在 DNS 污染。")
        print("     我可以给程序加一个「DoH 解析 + 固定 IP 直连」模式，")
        print("     你就能像普通程序一样在本地跑，不用任何代理。")
        print(f"     请把这几个真实 IP 发我：{real_ips.get('jp.mercari.com')}")
        return 0

    if any(not polluted.get(h) for h in HOSTS):
        print("  ⚠️ DNS 本身没被污染，但 TLS/HTTP 不通。")
        print("     这说明是线路问题，换网络环境或走代理。")
    else:
        print("  ❌ 拿真实 IP 也过不去：DNS 污染可以绕过，「按 IP 地区拦截」绕不过。")
        print("     本地直连这条路走不通，需要把抓取放到海外。")
    print("     推荐：GitHub Actions 定时跑（零成本、不用 VPN、邮件通知照常）。")
    print(f"     真实 IP 记录：{real_ips}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
