"""变化判定：纯函数，方便单测。

两类监控各有一套判定：

* **L1（mode = "new"）**：重点是新商品出现。-- 已有商品若配了价格规则，也会顺带判价。
* **L2（mode = "price"）**：商品锁定后，只关心它的价格和状态。

判定只依赖「商品事实 + 该监控项的订阅记录」，不碰数据库，方便测试。
"""

from __future__ import annotations

from typing import Any

from .config import Watch
from .models import (
    STATUS_ON_SALE,
    STATUS_REMOVED,
    STATUS_SOLD,
    Event,
    Item,
)


def evaluate_new_listing(
    watch: Watch,
    item: Item,
    sub: dict[str, Any] | None,
    *,
    first_run: bool,
) -> list[Event]:
    """第一次见到这个商品时的判定。sub 为 None 表示是该监控项的新商品。"""
    if sub is not None:
        return []
    if not watch.filters.matches(item.name, item.price):
        return []
    if first_run and not watch.rules.alert_on_first_run:
        return []

    events: list[Event] = []
    rules = watch.rules

    if rules.new_item:
        events.append(
            Event(
                kind="new_item",
                watch=watch.name,
                item_id=item.id,
                title=item.name,
                url=item.url,
                price=item.price,
                detail=f"关键字「{watch.keyword}」新上架",
            )
        )
    if rules.below_price and item.price is not None and item.price <= rules.below_price:
        events.append(
            Event(
                kind="below_price",
                watch=watch.name,
                item_id=item.id,
                title=item.name,
                url=item.url,
                price=item.price,
                detail=f"低于目标价 ¥{rules.below_price:,}",
            )
        )
    return events


def evaluate_price_change(
    watch: Watch,
    item: Item,
    sub: dict[str, Any],
) -> list[Event]:
    """已有订阅的商品，判断价格变化。"""
    if not watch.filters.matches(item.name, item.price):
        return []

    rules = watch.rules
    old_price = sub.get("last_price")
    new_price = item.price
    if old_price is None or new_price is None or new_price == old_price:
        return []

    events: list[Event] = []
    title = item.name or sub.get("title", "")
    url = item.url or sub.get("url", "")

    if new_price < old_price:
        drop = old_price - new_price
        percent = drop / old_price * 100
        percent_hit = rules.price_drop_percent > 0 and percent >= rules.price_drop_percent
        absolute_hit = rules.price_drop_absolute > 0 and drop >= rules.price_drop_absolute
        any_drop = rules.price_drop_percent <= 0 and rules.price_drop_absolute <= 0
        if rules.price_drop and (percent_hit or absolute_hit or any_drop):
            events.append(
                Event(
                    kind="price_drop",
                    watch=watch.name,
                    item_id=item.id,
                    title=title,
                    url=url,
                    price=new_price,
                    old_price=old_price,
                    detail=f"¥{old_price:,} → ¥{new_price:,}（-{drop:,}，-{percent:.1f}%）",
                )
            )
    else:
        rise = new_price - old_price
        percent = rise / old_price * 100
        if rules.price_increase:
            events.append(
                Event(
                    kind="price_up",
                    watch=watch.name,
                    item_id=item.id,
                    title=title,
                    url=url,
                    price=new_price,
                    old_price=old_price,
                    detail=f"¥{old_price:,} → ¥{new_price:,}（+{rise:,}，+{percent:.1f}%）",
                )
            )

    if rules.below_price and new_price <= rules.below_price and not sub.get("below_alerted"):
        events.append(
            Event(
                kind="below_price",
                watch=watch.name,
                item_id=item.id,
                title=title,
                url=url,
                price=new_price,
                old_price=old_price,
                detail=f"跌破目标价 ¥{rules.below_price:,}",
            )
        )

    return events


def evaluate_status(
    watch: Watch,
    item_id: str,
    title: str,
    url: str,
    old_status: str | None,
    new_status: str,
    price: int | None = None,
) -> list[Event]:
    """售出 / 下架 / 重新上架。"""
    if not watch.rules.status_change:
        return []
    if not new_status or new_status == old_status:
        return []

    kind = None
    detail = ""
    if new_status == STATUS_SOLD:
        kind, detail = "sold_out", "商品已售出"
    elif new_status == STATUS_REMOVED:
        kind, detail = "removed", "商品已下架或被删除"
    elif new_status == STATUS_ON_SALE and old_status in {STATUS_SOLD, STATUS_REMOVED}:
        kind, detail = "back_on_sale", "商品重新上架"
    if kind is None:
        return []
    return [
        Event(
            kind=kind,
            watch=watch.name,
            item_id=item_id,
            title=title,
            url=url,
            price=price,
            detail=detail,
        )
    ]


def evaluate_lock(watch: Watch, item: Item) -> list[Event]:
    """L2 首次锁定一件商品时，告知用户锁的是哪一件。"""
    return [
        Event(
            kind="locked",
            watch=watch.name,
            item_id=item.id,
            title=item.name,
            url=item.url,
            price=item.price,
            detail=f"已锁定，当前价 ¥{item.price:,}" if item.price else "已锁定，价格未知",
        )
    ]
