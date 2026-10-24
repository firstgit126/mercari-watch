"""环境自检：一条命令说清楚「现在到底能不能跑」。

因为 Mercari 对网络环境敏感（地区限制、需要代理），先跑 doctor
再跑正式监控，能避免盲目等半天不知道问题在哪。
"""

from __future__ import annotations

import socket
import ssl
import sys
import time
import urllib.parse

from . import crypto, proxytest
from .config import Config
from .fetcher import ITEM_WEB, SEARCH_WEB, MercariClient, parse_items_from_html
from .notify import EmailNotifier

OK = "  [OK]  "
WARN = "  [WARN]"
FAIL = "  [FAIL]"


class _Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0
        self.hints: list[str] = []

    def ok(self, msg: str) -> None:
        print(f"{OK} {msg}")

    def warn(self, msg: str, hint: str = "") -> None:
        self.warnings += 1
        print(f"{WARN} {msg}")
        if hint:
            self.hints.append(hint)

    def fail(self, msg: str, hint: str = "") -> None:
        self.failures += 1
        print(f"{FAIL} {msg}")
        if hint:
            self.hints.append(hint)

    def info(self, msg: str) -> None:
        print(f"        {msg}")


def _dns_check(host: str, report: _Report) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({info[4][0] for info in infos})
        report.ok(f"DNS 解析 {host} -> {', '.join(ips[:4])}")
        if any(ip.startswith("199.59.150.") for ip in ips):
            report.fail(
                f"{host} 解析到了黑洞地址 199.59.150.x",
                "这是典型的 DNS 污染，说明当前网络无法直连 Mercari：需要开启代理（在 config.toml 的 network.proxy 填 http://127.0.0.1:端口），或换到能访问日本的网络环境。",
            )
        return ips
    except socket.gaierror as exc:
        report.fail(
            f"DNS 解析 {host} 失败：{exc}",
            "检查网络或代理设置；如果本机需要代理才能上网，请先在 config.toml 里填 network.proxy。",
        )
        return []


def _tls_check(host: str, report: _Report) -> bool:
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                report.ok(f"TLS 握手 {host} 正常（{tls.version()}）")
                return True
    except Exception as exc:  # noqa: BLE001
        report.fail(
            f"TLS 连接 {host} 失败：{type(exc).__name__}: {exc}",
            "如果域名解析正常但连不上，多半是网络被拦，请配置代理。",
        )
        return False


def run_doctor(
    cfg: Config, *, keyword: str | None = None, send_mail: bool = False, dump: bool = False
) -> int:
    report = _Report()
    print("=" * 66)
    print(" mercari-watch 环境自检")
    print("=" * 66)

    # 1) 运行时
    print("\n[1/6] 运行环境")
    report.ok(f"Python {sys.version.split()[0]}（{sys.executable}）")
    if sys.version_info < (3, 11):
        report.warn("Python 低于 3.11，内置 tomllib 不可用，需要额外安装 tomli")
    else:
        report.ok("tomllib 可用")
    report.ok(f"配置文件：{cfg.path}")
    report.ok(f"数据目录：{cfg.data_dir}")
    report.info(f"监控项：" + "、".join(w.name for w in cfg.watches))
    report.info(f"抓取模式：{cfg.network.fetch_mode}　代理：{cfg.network.proxy or '未设置'}")

    # 2) 签名
    print("\n[2/6] DPoP 签名（接口通道需要）")
    ok, detail = crypto.self_test()
    (report.ok if ok else report.fail)(f"ES256/DPoP 自检：{detail}")

    # 3) 网络
    print("\n[3/6] 网络连通性")
    if cfg.network.proxy:
        print(f"        代理：{proxytest.mask_proxy(cfg.network.proxy)}")
        try:
            proxy_result = proxytest.check(cfg.network, keyword or cfg.watches[0].keyword)
            level, lines = proxytest.verdict(proxy_result)
            for line in lines:
                report.info(line)
            if level == "ok":
                report.ok("代理可用，并且能从煤炉拿到商品")
            elif level == "warn":
                report.warn("代理基本可用，但有需要留意的地方")
            else:
                report.fail(
                    "代理不可用",
                    "换一个日本节点的代理；拿到新地址后可以用 `proxy-test --proxy ...` 单独复验，不用改配置。",
                )
        except Exception as exc:  # noqa: BLE001
            report.fail(f"代理体检出错：{type(exc).__name__}: {exc}")
    else:
        ips = _dns_check("jp.mercari.com", report)
        if ips:
            _tls_check("jp.mercari.com", report)

    # 4) 网页搜索通道
    print("\n[4/6] 搜索页抓取与解析")
    client = MercariClient(cfg.network, cfg.data_dir)
    target_keyword = keyword or cfg.watches[0].keyword
    report.info(f"测试关键字：{target_keyword}")
    started = time.time()
    try:
        fake_watch = cfg.watches[0]
        if keyword:
            from dataclasses import replace

            fake_watch = replace(fake_watch, keyword=keyword, name=f"doctor:{keyword}")
        params = urllib.parse.urlencode(
            {"keyword": target_keyword, "status": "on_sale", "sort": "created_time", "order": "desc"}
        )
        url = f"{SEARCH_WEB}?{params}"
        resp = client.http.request("GET", url, headers={"Referer": "https://jp.mercari.com/"})
        elapsed = time.time() - started
        report.ok(f"HTTP {resp.status}，{len(resp.body)} 字节，{elapsed:.1f}s")
        if dump:
            dump_dir = cfg.data_dir / "dumps"
            dump_dir.mkdir(parents=True, exist_ok=True)
            dump_path = dump_dir / f"doctor_{int(time.time())}.html"
            dump_path.write_text(resp.text, "utf-8", errors="replace")
            report.info(f"原始页面已保存：{dump_path}")
        items = parse_items_from_html(resp.text)
        if items:
            report.ok(f"解析出 {len(items)} 个商品")
            for item in items[:3]:
                price = f"¥{item.price:,}" if item.price else "价格未知"
                report.info(f"{item.id}  {price}  {item.name[:38]}")
        else:
            report.fail(
                "页面拿到了，但一个商品都没解析出来",
                "多半是页面结构变了。请用 `python -m mercari_watch doctor --dump` 保存原始 HTML，把文件发给开发者适配解析规则。",
            )
            dump_dir = cfg.data_dir / "dumps"
            dump_dir.mkdir(parents=True, exist_ok=True)
            path = dump_dir / f"doctor_{int(time.time())}.html"
            path.write_text(resp.text, "utf-8", errors="replace")
            report.info(f"原始页面已保存：{path}")
    except Exception as exc:  # noqa: BLE001
        report.fail(
            f"搜索页请求失败：{type(exc).__name__}: {exc}",
            "如果是超时/连接被重置，基本可以确定需要代理；配好代理后重新运行 doctor。",
        )
        items = []

    # 5) 详情页状态解析
    print("\n[5/6] 商品详情页状态解析")
    if items:
        sample = items[0]
        try:
            html = client.http.request(
                "GET", ITEM_WEB.format(item_id=sample.id), headers={"Referer": "https://jp.mercari.com/"}
            ).text
            from .fetcher import parse_status_from_item_page

            status = parse_status_from_item_page(html)
            if status == "unknown":
                report.warn(
                    f"详情页拿到了（{len(html)} 字节），但没解析出状态",
                    "状态变化提醒可能失效，其余提醒不受影响。",
                )
            else:
                report.ok(f"示例商品 {sample.id} 状态解析为：{status}")
        except Exception as exc:  # noqa: BLE001
            report.warn(f"详情页请求失败：{type(exc).__name__}: {exc}")
    else:
        report.warn("跳过（第 4 步没有解析出商品）")

    # 6) 邮件
    print("\n[6/6] 邮件通知")
    if not cfg.email.enabled:
        report.warn("未启用邮件通知（notify.email.enabled = false）")
    elif cfg.email.problems():
        report.fail("邮件配置不完整：" + "、".join(cfg.email.problems()))
    else:
        report.ok(f"SMTP {cfg.email.host}:{cfg.email.port}（{cfg.email.security}）")
        report.info(f"收件人：{', '.join(cfg.email.to_addrs)}")
        if send_mail:
            try:
                EmailNotifier(cfg.email).send_test()
                report.ok("测试邮件已发出，请查收（含垃圾箱）")
            except Exception as exc:  # noqa: BLE001
                report.fail(
                    f"测试邮件发送失败：{exc}",
                    "QQ/163 邮箱要用「授权码」而不是登录密码；端口 465 配 security=ssl，587 配 starttls。",
                )
        else:
            report.info("加 --send-mail 可以真发一封测试邮件")

    # 汇总
    print("\n" + "=" * 66)
    if report.failures == 0:
        print(f" 结论：可以运行（{report.warnings} 个提醒）")
        if report.warnings:
            for hint in report.hints:
                print(f"   - {hint}")
    else:
        print(f" 结论：有 {report.failures} 个阻塞问题需要先解决")
        for hint in report.hints:
            print(f"   - {hint}")
    print("=" * 66)
    return 0 if report.failures == 0 else 1
