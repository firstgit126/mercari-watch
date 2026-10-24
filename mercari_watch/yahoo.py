"""雅虎日拍（ヤフオク）抓取。

和煤炉不同，日拍的搜索结果直接服务端渲染在 ``__NEXT_DATA__`` 里，
字段齐全（auctionId / title / price / buyNowPrice / endTime / imageUrl），
所以不需要签名，一次 GET 就能拿到整页。

字段说明：
* ``price``        —— 当前价格（有出价后是最高出价，否则是起拍价）
* ``buyNowPrice``  —— 即决价，没有则为 0
* ``endTime``      —— 结束时间（ISO，带 +09:00）
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Network, Watch
from .fetcher import SearchResult, normalize_keyword
from .http import HttpClient, HttpError
from .models import (
    Item,
    STATUS_ON_SALE,
    STATUS_REMOVED,
    STATUS_SOLD,
    STATUS_UNKNOWN,
    normalize_status,
)

log = logging.getLogger(__name__)

SEARCH_WEB = "https://auctions.yahoo.co.jp/search/search"
ITEM_WEB = "https://auctions.yahoo.co.jp/jp/auction/{item_id}"
PAGE_SIZE = 50

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _to_epoch(value) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


def _walk_items(node, out: list[dict], depth: int = 0) -> None:
    if depth > 14:
        return
    if isinstance(node, dict):
        if "auctionId" in node and "title" in node and ("price" in node or "currentPrice" in node):
            out.append(node)
            return
        for value in node.values():
            _walk_items(value, out, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _walk_items(value, out, depth + 1)


def _normalize(raw: dict) -> Item | None:
    auction_id = raw.get("auctionId")
    if not isinstance(auction_id, str) or not auction_id:
        return None
    price = raw.get("price") or raw.get("currentPrice")
    if isinstance(price, str):
        digits = re.sub(r"[^\d]", "", price)
        price = int(digits) if digits else None
    title = raw.get("title") or ""
    return Item(
        id=auction_id,
        name=title if isinstance(title, str) else str(title),
        price=price if isinstance(price, int) and price > 0 else None,
        status=STATUS_ON_SALE,
        url=ITEM_WEB.format(item_id=auction_id),
        thumb=raw.get("imageUrl") or "",
        seller=str(raw.get("seller") or ""),
        created=_to_epoch(raw.get("startTime")),
    )


def _load_next_data(html: str):
    match = _NEXT_DATA.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def parse_items_from_html(html: str) -> list[Item]:
    data = _load_next_data(html)
    if data is None:
        return []
    raw_items: list[dict] = []
    _walk_items(data, raw_items)
    seen: set[str] = set()
    items: list[Item] = []
    for raw in raw_items:
        item = _normalize(raw)
        if item is None or item.id in seen:
            continue
        seen.add(item.id)
        items.append(item)
    return items


def parse_item_status(html: str, item_id: str) -> tuple[str, int | None, str]:
    """从商品页解析 (状态, 当前价, 标题)。"""
    data = _load_next_data(html)
    if data is None:
        return STATUS_UNKNOWN, None, ""
    raw_items: list[dict] = []
    _walk_items(data, raw_items)
    target = next((r for r in raw_items if r.get("auctionId") == item_id), None)
    if target is None and raw_items:
        target = raw_items[0]
    if target is None:
        return STATUS_UNKNOWN, None, ""

    item = _normalize(target)
    price = item.price if item else None
    title = item.name if item else ""

    end_epoch = _to_epoch(target.get("endTime"))
    if end_epoch is not None and end_epoch < time.time():
        # 已结束：有出价算成交，没出价算流拍
        won = bool(target.get("bidCount"))
        return (STATUS_SOLD if won else STATUS_REMOVED), price, title

    raw_status = target.get("status") or target.get("auctionStatus")
    if raw_status:
        mapped = normalize_status(raw_status)
        if mapped != STATUS_UNKNOWN:
            return mapped, price, title
    return STATUS_ON_SALE, price, title


@dataclass
class _Dummy:
    pass


class YahooClient:
    """和 MercariClient 同一套接口，方便 runner 直接换着用。"""

    def __init__(self, network: Network, data_dir: Path) -> None:
        self.network = network
        self.data_dir = data_dir
        self.http = HttpClient(
            proxy=network.proxy,
            timeout=network.timeout,
            retries=network.retries,
            user_agent=network.user_agent,
            ca_bundle=network.ca_bundle,
        )

    def _dump(self, tag: str, content: str) -> None:
        if not self.network.dump_html:
            return
        dump_dir = self.data_dir / "dumps"
        dump_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w\-]+", "_", tag)[:60]
        (dump_dir / f"{int(time.time())}_{safe}.html").write_text(
            content, "utf-8", errors="replace"
        )

    def search(self, watch: Watch, page_token: str = "") -> SearchResult:
        try:
            offset = int(page_token or 1)
        except ValueError:
            offset = 1
        params = {
            "p": normalize_keyword(watch.keyword),
            "ei": "UTF-8",
            "s1": "new",       # 按上架时间排序
            "o1": "d",         # 倒序：新的在前
            "n": PAGE_SIZE,
            "b": offset,
            "sc": "a",
        }
        url = f"{SEARCH_WEB}?{urllib.parse.urlencode(params)}"
        resp = self.http.request(
            "GET",
            url,
            headers={
                "Referer": "https://auctions.yahoo.co.jp/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        html = resp.text
        self._dump(f"yahoo_search_{watch.name}", html)
        items = parse_items_from_html(html)
        next_token = str(offset + PAGE_SIZE) if len(items) >= PAGE_SIZE else ""
        if not items:
            log.warning("日拍搜索页没有解析出商品（%d 字节）", len(html))
        return SearchResult(items=items, next_token=next_token, mode="yahoo", raw_len=len(html))

    def fetch_item(self, item_id: str) -> Item | None:
        url = ITEM_WEB.format(item_id=item_id)
        try:
            resp = self.http.request("GET", url, headers={"Referer": "https://auctions.yahoo.co.jp/"})
        except HttpError as exc:
            if exc.status in (404, 410):
                return None
            raise
        status, price, title = parse_item_status(resp.text, item_id)
        self._dump(f"yahoo_item_{item_id}", resp.text)
        return Item(
            id=item_id,
            name=title,
            price=price,
            status=status,
            url=url,
        )
