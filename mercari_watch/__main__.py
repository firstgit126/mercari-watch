"""命令行入口。

    python -m mercari_watch init                 # 生成 config.toml 和 data 目录
    python -m mercari_watch doctor               # 环境自检（第一次一定要跑）
    python -m mercari_watch once                 # 只跑一轮，看结果
    python -m mercari_watch run                  # 常驻监控
    python -m mercari_watch mail-test            # 单独测一封邮件
    python -m mercari_watch status               # 看当前状态和最近事件
    python -m mercari_watch parse page.html      # 离线验证解析器
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

from . import __version__, proxytest
from .config import Config, ConfigError, load_config
from .doctor import run_doctor
from .fetcher import MercariClient, parse_items_from_html
from .logutil import setup_logging
from .models import STATUS_LABEL
from .notify import EmailNotifier
from .runner import Runner
from .store import Store

DEFAULT_CONFIG = "config.toml"


def _find_config(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    candidate = Path.cwd() / DEFAULT_CONFIG
    if candidate.exists():
        return candidate
    beside_package = Path(__file__).resolve().parent.parent / DEFAULT_CONFIG
    if beside_package.exists():
        return beside_package
    return candidate


def _build(path: Path, proxy: str | None = None):
    cfg = _apply_proxy(load_config(path), proxy)
    setup_logging(cfg.log_level, cfg.log_path)
    store = Store(cfg.db_path)
    client = MercariClient(cfg.network, cfg.data_dir)
    notifier = EmailNotifier(cfg.email)
    return cfg, store, client, notifier, Runner(cfg, store, client, notifier)


def _apply_proxy(cfg: Config, proxy: str | None) -> Config:
    """命令行 --proxy 临时覆盖配置里的代理，方便买代理时反复试。"""
    if not proxy:
        return cfg
    return replace(cfg, network=replace(cfg.network, proxy=proxy))


def cmd_init(args: argparse.Namespace) -> int:
    target_dir = Path(args.dir or Path.cwd()).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    config_path = target_dir / DEFAULT_CONFIG
    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    if config_path.exists() and not args.force:
        print(f"配置文件已存在，未覆盖：{config_path}")
    else:
        if not example.exists():
            print(f"找不到模板：{example}", file=sys.stderr)
            return 1
        shutil.copyfile(example, config_path)
        print(f"已生成配置文件：{config_path}")
    (target_dir / "data").mkdir(exist_ok=True)
    print("\n下一步：")
    print(f"  1. 编辑 {config_path}，改关键字和邮箱")
    print(f"  2. cd {target_dir} && python -m mercari_watch doctor")
    print("  3. python -m mercari_watch once")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _apply_proxy(load_config(_find_config(args.config)), args.proxy)
    setup_logging("WARNING")
    return run_doctor(cfg, keyword=args.keyword, send_mail=args.send_mail, dump=args.dump)


def cmd_proxy_test(args: argparse.Namespace) -> int:
    cfg = _apply_proxy(load_config(_find_config(args.config)), args.proxy)
    setup_logging("WARNING")
    if not cfg.network.proxy:
        print("还没有配置代理。可以这样临时试一个：", file=sys.stderr)
        print(
            '  python -m mercari_watch proxy-test --proxy "http://user:pass@1.2.3.4:8080"',
            file=sys.stderr,
        )
        return 2

    keyword = args.keyword or cfg.watches[0].keyword
    print("=" * 62)
    print(" 代理体检")
    print("=" * 62)
    print(f"  代理    ：{proxytest.mask_proxy(cfg.network.proxy)}")
    print(f"  关键字  ：{keyword}")
    print()

    result = proxytest.check(cfg.network, keyword)
    level, lines = proxytest.verdict(result)
    for line in lines:
        print(f"  · {line}")

    print()
    if level == "ok":
        print(f"  结论：这个代理可以！{result['elapsed']:.1f} 秒拿到 {result['items']} 个商品。")
        print(f'  把 network.proxy = "{cfg.network.proxy}" 写进 config.toml 即可。')
        return 0
    if level == "warn":
        print("  结论：基本可用，但上面标了注意点，先用 `once` 跑一轮验证。")
        return 0
    print("  结论：这个代理不行，换一个（优先选日本节点）。")
    return 1


def cmd_once(args: argparse.Namespace) -> int:
    cfg, store, client, notifier, runner = _build(_find_config(args.config), args.proxy)
    try:
        reports = runner.run_once()
        print("\n本轮结果")
        print("-" * 60)
        for rep in reports:
            status = "出错" if rep.error else "正常"
            mode = "L1找新" if rep.mode == "new" else "L2盯价"
            print(
                f"  {rep.watch:<22} {mode}  记录 {rep.fetched:>3} 条  "
                f"变化 {rep.new_events:>2} 条  状态复查 {rep.status_checked:>2} 个  [{status}]"
            )
            if rep.error:
                print(f"      {rep.error}")
        return 1 if any(r.error for r in reports) else 0
    finally:
        store.close()


def cmd_run(args: argparse.Namespace) -> int:
    cfg, store, client, notifier, runner = _build(_find_config(args.config), args.proxy)
    stop = [False]
    try:
        runner.run_forever(stop)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        stop[0] = True
        store.close()
    return 0


def cmd_mail_test(args: argparse.Namespace) -> int:
    cfg = load_config(_find_config(args.config))
    setup_logging(cfg.log_level)
    if not cfg.email.enabled:
        print("邮件通知没启用（notify.email.enabled = false）", file=sys.stderr)
        return 1
    try:
        EmailNotifier(cfg.email).send_test()
        print(f"测试邮件已发送至：{', '.join(cfg.email.to_addrs)}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"发送失败：{exc}", file=sys.stderr)
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(_find_config(args.config))
    store = Store(cfg.db_path)
    try:
        print(f"配置文件：{cfg.path}")
        print(f"数据库：  {cfg.db_path}")
        print()
        for watch in cfg.watches:
            state = store.watch_state(watch.name)
            count = store.count_subs(watch.name)
            locked = store.locked_subs(watch.name) if watch.mode == "price" else []
            last = state["last_run"]
            last_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "从未"
            print(f"[{watch.name}]")
            print(f"  类型     ：{'L2 盯价格' if watch.mode == 'price' else 'L1 找新货'}")
            print(f"  关键字   ：{watch.keyword}")
            print(f"  间隔     ：{watch.interval} 秒　抓取页数：{watch.pages}")
            print(f"  已记录   ：{count} 个商品")
            if watch.mode == "price":
                if locked:
                    for sub in locked:
                        print(
                            f"  锁定中   ：{sub['item_id']}  ¥{sub['price']:,}  "
                            f"{(sub['title'] or '')[:30]}"
                        )
                else:
                    print("  锁定中   ：（尚未锁定，下一轮会搜索并锁定第一件）")
            print(f"  基线     ：{'已建立' if state['baseline_done'] else '未建立'}")
            print(f"  上次运行 ：{last_text}")
            print()
        events = store.recent_events(10)
        if events:
            print("最近事件")
            for row in events:
                ts = time.strftime("%m-%d %H:%M", time.localtime(row["ts"]))
                flag = "" if row["notified"] else "（未发送）"
                print(f"  {ts}  {row['kind']:<12} {row['title'][:34] if row['title'] else row['item_id']}  {flag}")
        else:
            print("还没有任何事件")
    finally:
        store.close()
    return 0


def cmd_relock(args: argparse.Namespace) -> int:
    """解除 L2 的锁定，下一轮会重新搜索并锁定第一件（用来换目标）。"""
    cfg = load_config(_find_config(args.config))
    store = Store(cfg.db_path)
    try:
        price_watches = [w.name for w in cfg.watches if w.mode == "price"]
        if args.watch:
            if args.watch not in [w.name for w in cfg.watches]:
                print(f"没有这个监控项：{args.watch}", file=sys.stderr)
                print("可用：" + "、".join(w.name for w in cfg.watches), file=sys.stderr)
                return 2
            targets = [args.watch]
        else:
            targets = price_watches
        if not targets:
            print("没有 mode = \"price\" 的监控项，无需解锁")
            return 0
        total = 0
        for name in targets:
            count = store.unlock(name)
            total += count
            print(f"  {name}：解除 {count} 个锁定")
        print(f"共解除 {total} 个。下一轮会重新搜索并锁定第一件符合条件的商品。")
        return 0
    finally:
        store.close()


def cmd_parse(args: argparse.Namespace) -> int:
    path = Path(args.file).expanduser().resolve()
    if not path.exists():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 1
    html = path.read_text("utf-8", errors="replace")
    items = parse_items_from_html(html)
    print(f"从 {path.name}（{len(html)} 字节）解析出 {len(items)} 个商品")
    for item in items[: args.limit]:
        price = f"¥{item.price:,}" if item.price else "价格未知"
        print(f"  {item.id}  {price:>10}  {STATUS_LABEL.get(item.status, item.status):<4}  {item.name[:50]}")
    return 0 if items else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mercari_watch",
        description="Mercari（煤炉）关键字监控：新上架 / 降价 / 售出，邮件通知",
    )
    parser.add_argument("--version", action="version", version=f"mercari-watch {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config(p: argparse.ArgumentParser) -> None:
        p.add_argument("-c", "--config", help="配置文件路径，默认 ./config.toml")

    p_init = sub.add_parser("init", help="生成配置文件和 data 目录")
    p_init.add_argument("--dir", help="在哪个目录初始化，默认当前目录")
    p_init.add_argument("--force", action="store_true", help="覆盖已有 config.toml")
    p_init.set_defaults(func=cmd_init)

    p_doctor = sub.add_parser("doctor", help="环境自检：网络、解析、邮件")
    add_config(p_doctor)
    p_doctor.add_argument("--keyword", help="用指定关键字测试")
    p_doctor.add_argument("--send-mail", action="store_true", help="真发一封测试邮件")
    p_doctor.add_argument("--dump", action="store_true", help="保存抓到的原始 HTML 便于排查")
    p_doctor.add_argument("--proxy", help="临时指定代理，不写进配置也能测")
    p_doctor.set_defaults(func=cmd_doctor)

    p_proxy = sub.add_parser("proxy-test", help="代理体检：能不能真的访问煤炉")
    add_config(p_proxy)
    p_proxy.add_argument("--proxy", help="例如 http://user:pass@1.2.3.4:8080")
    p_proxy.add_argument("--keyword", help="用哪个关键字测，默认取第一个监控项")
    p_proxy.set_defaults(func=cmd_proxy_test)

    p_once = sub.add_parser("once", help="只跑一轮")
    add_config(p_once)
    p_once.add_argument("--proxy", help="临时指定代理")
    p_once.set_defaults(func=cmd_once)

    p_run = sub.add_parser("run", help="常驻监控")
    add_config(p_run)
    p_run.add_argument("--proxy", help="临时指定代理")
    p_run.set_defaults(func=cmd_run)

    p_mail = sub.add_parser("mail-test", help="发一封测试邮件")
    add_config(p_mail)
    p_mail.set_defaults(func=cmd_mail_test)

    p_status = sub.add_parser("status", help="查看状态和最近事件")
    add_config(p_status)
    p_status.set_defaults(func=cmd_status)

    p_relock = sub.add_parser("relock", help="L2 解除锁定，换一件盯")
    add_config(p_relock)
    p_relock.add_argument("watch", nargs="?", help="监控项名称；省略则处理所有 L2")
    p_relock.set_defaults(func=cmd_relock)

    p_parse = sub.add_parser("parse", help="离线解析一个保存下来的 HTML")
    p_parse.add_argument("file")
    p_parse.add_argument("--limit", type=int, default=20)
    p_parse.set_defaults(func=cmd_parse)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
