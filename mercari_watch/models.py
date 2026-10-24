"""数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field

# 商品状态（内部统一值）
STATUS_ON_SALE = "on_sale"
STATUS_SOLD = "sold_out"
STATUS_TRADING = "trading"
STATUS_REMOVED = "removed"
STATUS_UNKNOWN = "unknown"

_STATUS_MAP = {
    "ITEM_STATUS_ON_SALE": STATUS_ON_SALE,
    "ITEM_STATUS_SOLD_OUT": STATUS_SOLD,
    "ITEM_STATUS_TRADING": STATUS_TRADING,
    "ITEM_STATUS_CANCEL": STATUS_REMOVED,
    "on_sale": STATUS_ON_SALE,
    "sold_out": STATUS_SOLD,
    "trading": STATUS_TRADING,
    "removed": STATUS_REMOVED,
}

STATUS_LABEL = {
    STATUS_ON_SALE: "在售",
    STATUS_SOLD: "已售出",
    STATUS_TRADING: "交易中",
    STATUS_REMOVED: "已下架",
    STATUS_UNKNOWN: "未知",
}


def normalize_status(raw: str | None) -> str:
    if not raw:
        return STATUS_UNKNOWN
    return _STATUS_MAP.get(raw, STATUS_UNKNOWN)


@dataclass(slots=True)
class Item:
    id: str
    name: str
    price: int | None = None
    status: str = STATUS_ON_SALE
    url: str = ""
    thumb: str = ""
    seller: str = ""
    created: int | None = None

    def __post_init__(self) -> None:
        if not self.url:
            self.url = item_url(self.id)


def item_url(item_id: str) -> str:
    return f"https://jp.mercari.com/item/{item_id}"


@dataclass(slots=True)
class Event:
    # new_item | price_drop | price_up | below_price | sold_out | removed | back_on_sale | locked
    kind: str
    watch: str
    item_id: str
    title: str
    url: str
    price: int | None = None
    old_price: int | None = None
    detail: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def kind_label(self) -> str:
        return {
            "new_item": "新上架",
            "price_drop": "降价",
            "price_up": "涨价",
            "below_price": "跌破目标价",
            "sold_out": "已售出",
            "removed": "已下架",
            "back_on_sale": "重新上架",
            "locked": "已锁定",
        }.get(self.kind, self.kind)
