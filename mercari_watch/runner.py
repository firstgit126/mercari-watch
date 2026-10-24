"""主循环：按各自间隔轮询、判定、发信。

两种监控项走不同路径：

* ``mode = "new"``（L1）：每轮搜索，发现新商品就报；稳态只抓第 1 页。
* ``mode = "price"``（L2）：先搜索锁定一件商品，之后只盯这一件。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

from .config import Config, Watch
from .fetcher import MercariClient
from .models import (
    STATUS_REMOVED,
    STATUS_SOLD,
    STATUS_UNKNOWN,
    Event,
)
from .notify import EmailNotifier
from .rules import (
    evaluate_lock,
    evaluate_new_listing,
    evaluate_price_change,
    evaluate_status,
)
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    watch: str
    mode: str
    fetched: int
    new_events: int
    status_checked: int = 0
    locked: int = 0
    error: str = ""


class Runner:
    def __init__(self, cfg: Config, store: Store, client: MercariClient, notifier: EmailNotifier):
        self.cfg = cfg
        self.store = store
        self.client = client
        self.notifier = notifier

    # -- 分发 --------------------------------------------------------------
    def poll_watch(self, watch: Watch) -> CycleReport:
        if watch.mode == "price":
            return self.poll_price_watch(watch)
        return self.poll_new_watch(watch)

    def _finish(self, watch: Watch, events: list[Event], report: CycleReport) -> CycleReport:
        for event in events:
            self.store.add_event(event)
            if event.kind == "below_price":
                self.store.mark_below_alerted(event.watch, event.item_id)
        report.new_events = len(events)
        if events:
            log.info("[%s] 本轮判定出 %d 条变化：%s", watch.name, len(events),
                     "、".join(e.kind_label for e in events))
        return report

    # -- L1：找新货 --------------------------------------------------------
    def poll_new_watch(self, watch: Watch) -> CycleReport:
        state = self.store.watch_state(watch.name)
        first_run = not state["baseline_done"]
        if first_run:
            log.info("[%s] 首次运行，翻完 %d 页建立基线（不逐条报老货）", watch.name, watch.pages)

        # 建基线要翻完所有页；稳态只看第 1 页，新货一定在最前面
        pages = watch.pages if first_run else 1
        items, token = [], ""
        last_count = 0
        pages_fetched = 0
        for page in range(pages):
            result = self.client.search(watch, token)
            items.extend(result.items)
            last_count = len(result.items)
            pages_fetched += 1
            log.info("[%s] 第 %d 页：%d 条（通道 %s）", watch.name, page + 1, len(result.items), result.mode)
            token = result.next_token
            if not token:
                break
            time.sleep(random.uniform(1.0, 2.0))
        if first_run and pages_fetched < pages and last_count >= 20:
            log.warning(
                "[%s] 只拿到 %d/%d 页且没找到翻页标记。若第二轮突然报一堆老货，"
                "说明基线没覆盖完，把该监控项的 pages 改成 1 即可",
                watch.name, pages_fetched, pages,
            )

        events: list[Event] = []
        seen: set[str] = set()
        for item in items:
            if item.id in seen:
                continue
            seen.add(item.id)
            self.store.upsert_item(item)
            sub = self.store.get_sub(watch.name, item.id)
            if sub is None:
                events.extend(evaluate_new_listing(watch, item, None, first_run=first_run))
                self.store.subscribe(watch.name, item.id)
                if (
                    first_run
                    and not watch.rules.alert_on_first_run
                    and watch.rules.below_price
                    and item.price is not None
                    and item.price <= watch.rules.below_price
                ):
                    self.store.mark_below_alerted(watch.name, item.id)
            else:
                events.extend(evaluate_price_change(watch, item, sub))
                self.store.update_sub(watch.name, item.id, price=item.price)

        status_events, checked = self.check_statuses(watch)
        events.extend(status_events)

        if first_run:
            self.store.mark_baseline(watch.name)
        self.store.touch_watch(watch.name, len(seen))
        return self._finish(
            watch, events, CycleReport(watch.name, "new", len(seen), 0, checked)
        )

    # -- L2：盯价格 --------------------------------------------------------
    def poll_price_watch(self, watch: Watch) -> CycleReport:
        self.store.watch_state(watch.name)  # 确保 watches 表里有这条记录
        events: list[Event] = []
        locked = self.store.locked_subs(watch.name)

        if not locked:
            events.extend(self._lock_target(watch))
            self.store.touch_watch(watch.name, 1 if events else 0)
            return self._finish(watch, events, CycleReport(watch.name, "price", 0, 0, 0, 1))

        checked = 0
        for sub in locked:
            item_id = sub["item_id"]
            if sub["status"] == STATUS_REMOVED:
                continue  # 已经确认没了，别再浪费请求

            try:
                latest = self.client.fetch_item(item_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] 查询 %s 失败：%s", watch.name, item_id, exc)
                continue
            checked += 1

            if latest is None:
                events.extend(
                    evaluate_status(
                        watch, item_id, sub["title"] or "", sub["url"] or "",
                        sub["status"], STATUS_REMOVED, sub["price"],
                    )
                )
                self.store.set_item_status(item_id, STATUS_REMOVED)
                log.info("[%s] 锁定商品 %s 已下架/删除", watch.name, item_id)
                continue

            if latest.status == STATUS_UNKNOWN:
                latest.status = sub["status"]  # 解析不出就别覆盖
            events.extend(
                evaluate_status(
                    watch, item_id, sub["title"] or latest.name, sub["url"] or latest.url,
                    sub["status"], latest.status, latest.price if latest.price is not None else sub["price"],
                )
            )
            self.store.upsert_item(latest)
            if latest.price is not None:
                events.extend(evaluate_price_change(watch, latest, sub))
            self.store.update_sub(watch.name, item_id, price=latest.price, checked=True)
            time.sleep(random.uniform(0.8, 1.8))

        self.store.touch_watch(watch.name, len(locked))
        return self._finish(
            watch, events, CycleReport(watch.name, "price", len(locked), 0, checked, len(locked))
        )

    def _lock_target(self, watch: Watch) -> list[Event]:
        """搜索并锁定第一件符合条件的商品。"""
        result = self.client.search(watch, "")
        candidates = [i for i in result.items if watch.filters.matches(i.name, i.price)]
        if not candidates:
            log.warning(
                "[%s] 搜索「%s」没有找到符合条件的商品（共 %d 条结果），下一轮再试",
                watch.name, watch.keyword, len(result.items),
            )
            return []
        target = candidates[0]
        self.store.upsert_item(target)
        self.store.subscribe(watch.name, target.id, locked=True)
        self.store.mark_baseline(watch.name)
        log.info(
            "[%s] 已锁定 %s ｜ ¥%s ｜ %s",
            watch.name, target.id, target.price, target.name[:40],
        )
        return evaluate_lock(watch, target)

    # -- 状态复查（L1 用；L2 每轮本来就查了商品页） -------------------------
    def check_statuses(self, watch: Watch) -> tuple[list[Event], int]:
        if not watch.rules.status_change:
            return [], 0
        due = self.store.subs_due_for_status(
            watch.name, self.cfg.network.status_poll_batch, self.cfg.network.status_poll_interval
        )
        events: list[Event] = []
        checked = 0
        for row in due:
            item_id = row["item_id"]
            try:
                latest = self.client.fetch_item(item_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] 查询商品 %s 状态失败：%s", watch.name, item_id, exc)
                continue
            checked += 1
            if latest is None:
                events.extend(
                    evaluate_status(
                        watch, item_id, row["title"] or "", row["url"] or "",
                        row["status"], STATUS_REMOVED, row["price"],
                    )
                )
                self.store.set_item_status(item_id, STATUS_REMOVED)
                self.store.update_sub(watch.name, item_id, price=None, checked=True)
                continue
            if latest.status == STATUS_UNKNOWN:
                self.store.update_sub(watch.name, item_id, price=None, checked=True)
                continue
            events.extend(
                evaluate_status(
                    watch, item_id, row["title"] or latest.name, row["url"] or latest.url,
                    row["status"], latest.status, row["price"],
                )
            )
            self.store.set_item_status(item_id, latest.status)
            self.store.update_sub(watch.name, item_id, price=None, checked=True)
            time.sleep(random.uniform(0.8, 1.8))
        return events, checked

    # -- 汇总发信 ----------------------------------------------------------
    def flush_notifications(self) -> int:
        pending = self.store.pending_events()
        if not pending:
            return 0
        events = [
            Event(
                kind=row["kind"],
                watch=row["watch"],
                item_id=row["item_id"],
                title=row["title"] or "",
                url=row["url"] or "",
                price=row["price"],
                old_price=row["old_price"],
                detail=row["detail"] or "",
            )
            for row in pending
        ]
        # 多个关键字可能命中同一件商品（比如同时含ガレージキット和レジンキット），
        # 同一批次里同种事件只保留第一条，避免一封邮件里重复两次。
        seen: set[tuple[str, str]] = set()
        unique: list[Event] = []
        for event in events:
            key = (event.item_id, event.kind)
            if key in seen:
                continue
            seen.add(key)
            unique.append(event)
        dropped = len(events) - len(unique)
        if dropped:
            log.info("合并了 %d 条重复提醒（同一商品被多个关键字命中）", dropped)

        for event in unique:
            log.info(
                "[提醒] %s ｜ %s ｜ ¥%s ｜ %s",
                event.kind_label, event.title or event.item_id, event.price, event.url,
            )
        if self.notifier.enabled:
            try:
                self.notifier.send_events(unique)
            except Exception as exc:  # noqa: BLE001
                log.error("发信失败，事件保留到下一轮重试：%s", exc)
                return 0
        self.store.mark_events_notified([row["id"] for row in pending])
        return len(unique)

    # -- 调度 --------------------------------------------------------------
    def run_once(self) -> list[CycleReport]:
        reports = []
        for watch in self.cfg.watches:
            if not watch.enabled:
                continue
            try:
                reports.append(self.poll_watch(watch))
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] 本轮出错：%s", watch.name, exc)
                log.debug("详细堆栈", exc_info=True)
                reports.append(CycleReport(watch.name, watch.mode, 0, 0, error=str(exc)))
        self.flush_notifications()
        return reports

    def run_forever(self, stop_flag: "list[bool] | None" = None) -> None:
        watches = [w for w in self.cfg.watches if w.enabled]
        if not watches:
            log.error("没有启用的监控项")
            return
        next_due = {w.name: 0.0 for w in watches}
        failures = {w.name: 0 for w in watches}
        log.info("开始监控 %d 个监控项，Ctrl+C 停止", len(watches))

        while not (stop_flag and stop_flag[0]):
            now = time.time()
            for watch in [w for w in watches if next_due[w.name] <= now]:
                try:
                    self.poll_watch(watch)
                    failures[watch.name] = 0
                    delay = watch.interval + random.uniform(0, min(20, watch.interval * 0.1))
                except Exception as exc:  # noqa: BLE001
                    failures[watch.name] += 1
                    backoff = min(1800, 60 * 2 ** min(failures[watch.name], 5))
                    delay = max(watch.interval, backoff)
                    log.error(
                        "[%s] 第 %d 次失败，%d 秒后重试：%s",
                        watch.name, failures[watch.name], int(delay), exc,
                    )
                next_due[watch.name] = time.time() + delay

            self.flush_notifications()

            sleep_for = max(1.0, min(60.0, min(next_due.values()) - time.time()))
            deadline = time.time() + sleep_for
            while time.time() < deadline:
                if stop_flag and stop_flag[0]:
                    return
                time.sleep(min(0.5, max(0.0, deadline - time.time())))
