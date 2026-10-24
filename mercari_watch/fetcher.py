"""Mercari 数据抓取。

两条通道，互为备份：

* ``html`` —— 直接请求网页搜索页，从页面内嵌的 JSON / RSC 数据流里抠商品。
  不需要签名，最不容易被拦，是默认首选。
* ``api``  —— 请求 ``api.mercari.jp/v2/entities:search``，需要 DPoP 签名头。

网页结构随时可能变，所以解析器写得比较宽容：先找内嵌 JSON，
再扫 RSC 流，最后退回正则。``--dump-html`` 可以把原始页面存下来复查。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import crypto
from .config import Network, Watch
from .http import HttpClient, HttpError
from .models import (
    Item,
    STATUS_ON_SALE,
    STATUS_UNKNOWN,
    item_url,
    normalize_status,
)

log = logging.getLogger(__name__)

SEARCH_API = "https://api.mercari.jp/v2/entities:search"
SEARCH_WEB = "https://jp.mercari.com/search"
ITEM_WEB = "https://jp.mercari.com/item/{item_id}"

SORT_MAP = {
    "created_time": ("SORT_CREATED_TIME", "ORDER_DESC"),
    "price_asc": ("SORT_PRICE", "ORDER_ASC"),
    "price_desc": ("SORT_PRICE", "ORDER_DESC"),
    "score": ("SORT_SCORE", "ORDER_DESC"),
}

_SEARCH_ITEM_ID = re.compile(r'"id"\s*:\s*"(m\d{5,})"')
_NAME_RE = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.){1,400})"')
_PRICE_RE = re.compile(r'"price"\s*:\s*"?(\d{1,9})"?')
_STATUS_RE = re.compile(r'"status"\s*:\s*"(ITEM_STATUS_[A-Z_]+)"')


@dataclass
class SearchResult:
    items: list[Item] = field(default_factory=list)
    next_token: str = ""
    mode: str = "html"
    raw_len: int = 0


# ---------------------------------------------------------------------------
# 解析：页面 -> Item 列表
# ---------------------------------------------------------------------------
def normalize_keyword(keyword: str) -> str:
    """统一关键字里的空白。

    日文输入法常打出全角空格（U+3000），煤炉对它的处理不稳定，
    有时当作分隔符、有时搜不到东西。统一成半角单个空格最稳。
    """
    return " ".join(keyword.replace("\u3000", " ").split())


def _looks_like_item(node) -> bool:
    if not isinstance(node, dict):
        return False
    ident = node.get("id") or node.get("itemId") or node.get("item_id")
    if not isinstance(ident, str) or not ident.startswith("m"):
        return False
    if not ident[1:].isdigit() or len(ident) < 6:
        return False
    return any(key in node for key in ("name", "price", "title", "itemName"))


def _walk_items(node, out: list[dict], depth: int = 0) -> None:
    if depth > 20:
        return
    if isinstance(node, dict):
        if _looks_like_item(node):
            out.append(node)
            return
        for value in node.values():
            _walk_items(value, out, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _walk_items(value, out, depth + 1)


def _as_price(raw) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw) if raw > 0 else None
    if isinstance(raw, str):
        digits = re.sub(r"[^\d]", "", raw)
        if digits:
            value = int(digits)
            return value or None
    return None


def _as_epoch(raw) -> int | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = int(raw)
        if value > 10**12:  # 毫秒
            value //= 1000
        return value
    if isinstance(raw, str) and raw.isdigit():
        value = int(raw)
        if value > 10**12:
            value //= 1000
        return value
    return None


def _first_thumb(raw: dict) -> str:
    thumbs = raw.get("thumbnails") or raw.get("images") or raw.get("photos")
    if isinstance(thumbs, list) and thumbs:
        first = thumbs[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("uri") or first.get("url") or first.get("imageUrl") or ""
    for key in ("thumbnail", "imageUrl", "image", "photo"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def normalize_item(raw: dict, *, default_status: str = STATUS_UNKNOWN) -> Item | None:
    ident = raw.get("id") or raw.get("itemId") or raw.get("item_id")
    if not isinstance(ident, str) or not ident.startswith("m"):
        return None
    if not ident[1:].isdigit():
        return None
    name = raw.get("name") or raw.get("title") or raw.get("itemName") or ""
    if not isinstance(name, str):
        name = str(name)
    seller = raw.get("sellerId") or raw.get("seller_id") or ""
    return Item(
        id=ident,
        name=name.strip(),
        price=_as_price(raw.get("price")),
        status=normalize_status(raw.get("status")) if raw.get("status") else default_status,
        url=item_url(ident),
        thumb=_first_thumb(raw),
        seller=seller if isinstance(seller, str) else str(seller),
        created=_as_epoch(raw.get("created") or raw.get("createdTime") or raw.get("created_at")),
    )


def _unescape_flight(text: str) -> str:
    """把 RSC 数据流里的转义字符串还原成接近 JSON 的文本。"""
    out = []
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if ch == "\\" and idx + 1 < length:
            nxt = text[idx + 1]
            mapping = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r"}
            out.append(mapping.get(nxt, "\\" + nxt))
            idx += 2
            continue
        out.append(ch)
        idx += 1
    return "".join(out)


def iter_json_blobs(html: str):
    """产出页面里所有内嵌 JSON 文本块。"""
    for match in re.finditer(
        r'<script[^>]*type="application/(?:ld\+)?json"[^>]*>(.*?)</script>', html, re.S
    ):
        yield match.group(1)
    for match in re.finditer(r"<script[^>]*id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>", html, re.S):
        yield match.group(1)


def iter_flight_text(html: str):
    """产出 Next.js App Router 的 RSC 数据流文本。"""
    marker = "self.__next_f.push([1,"
    pos = 0
    while True:
        start = html.find(marker, pos)
        if start < 0:
            return
        idx = start + len(marker)
        while idx < len(html) and html[idx] in " \t\r\n":
            idx += 1
        if idx >= len(html) or html[idx] != '"':
            pos = start + len(marker)
            continue
        idx += 1
        buf = []
        while idx < len(html):
            ch = html[idx]
            if ch == "\\" and idx + 1 < len(html):
                buf.append(html[idx : idx + 2])
                idx += 2
                continue
            if ch == '"':
                break
            buf.append(ch)
            idx += 1
        yield _unescape_flight("".join(buf))
        pos = idx + 1


def _regex_items(text: str, limit: int = 120) -> list[Item]:
    """最后兜底：在原文里按 id/name/price 的位置关系抠商品。"""
    found: dict[str, Item] = {}
    for match in _SEARCH_ITEM_ID.finditer(text):
        item_id = match.group(1)
        if item_id in found:
            continue
        window = text[max(0, match.start() - 2000) : match.start() + 2000]
        name_match = _NAME_RE.search(window)
        price_match = _PRICE_RE.search(window)
        status_match = _STATUS_RE.search(window)
        found[item_id] = Item(
            id=item_id,
            name=(name_match.group(1) if name_match else "").replace('\\"', '"')[:200],
            price=_as_price(price_match.group(1)) if price_match else None,
            status=normalize_status(status_match.group(1)) if status_match else STATUS_ON_SALE,
            url=item_url(item_id),
        )
        if len(found) >= limit:
            break
    return list(found.values())


def parse_items_from_html(html: str) -> list[Item]:
    """从搜索页 HTML 里解析商品列表。"""
    raw_dicts: list[dict] = []
    for blob in iter_json_blobs(html):
        try:
            data = json.loads(blob)
        except Exception:  # noqa: BLE001
            continue
        _walk_items(data, raw_dicts)
    items = [it for it in (normalize_item(raw) for raw in raw_dicts) if it]
    if items:
        return _dedupe(items)

    flight_items: list[dict] = []
    for chunk in iter_flight_text(html):
        try:
            data = json.loads(chunk)
        except Exception:  # noqa: BLE001
            continue
        _walk_items(data, flight_items)
    items = [it for it in (normalize_item(raw) for raw in flight_items) if it]
    if items:
        return _dedupe(items)

    # 在 RSC 文本里直接扫 JSON 片段
    joined = "".join(iter_flight_text(html))
    if '"m' in joined:
        found = _regex_items(joined)
        if found:
            return _dedupe(found)

    return _dedupe(_regex_items(html))


def parse_next_token_from_html(html: str) -> str:
    patterns = [
        r'"nextPageToken"\s*:\s*"([^"]+)"',
        r'"next_page_token"\s*:\s*"([^"]+)"',
        r'page_token=([A-Za-z0-9_\-%]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return urllib.parse.unquote(match.group(1))
    return ""


def _dedupe(items: list[Item]) -> list[Item]:
    seen: set[str] = set()
    out: list[Item] = []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        out.append(item)
    return out


def parse_status_from_item_page(html: str) -> str:
    match = _STATUS_RE.search(html)
    if match:
        return normalize_status(match.group(1))
    for blob in iter_json_blobs(html):
        try:
            data = json.loads(blob)
        except Exception:  # noqa: BLE001
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if _looks_like_item(node) and node.get("status"):
                    return normalize_status(node["status"])
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    if 'sold' in html.lower():
        return "sold_out"
    return STATUS_UNKNOWN


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class MercariClient:
    def __init__(self, network: Network, data_dir: Path) -> None:
        self.network = network
        self.data_dir = data_dir
        self.http = HttpClient(
            proxy=network.proxy,
            timeout=network.timeout,
            retries=network.retries,
            user_agent=network.user_agent,
        )
        self._key: int | None = None
        self.mode = network.fetch_mode
        self._learned_mode: str | None = None

    # -- 密钥 --------------------------------------------------------------
    def _private_key(self) -> int:
        if self._key is not None:
            return self._key
        path = self.data_dir / "dpop_key.json"
        if path.exists():
            try:
                self._key = crypto.load_key(json.loads(path.read_text("utf-8")))
                return self._key
            except Exception:  # noqa: BLE001
                log.warning("DPoP 密钥文件损坏，重新生成")
        key = crypto.new_private_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(crypto.export_key(key), indent=2), "utf-8")
        self._key = key
        return key

    # -- 调试 --------------------------------------------------------------
    def _dump(self, tag: str, content: str) -> None:
        if not self.network.dump_html:
            return
        dump_dir = self.data_dir / "dumps"
        dump_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w\-]+", "_", tag)[:60]
        path = dump_dir / f"{int(time.time())}_{safe}.html"
        path.write_text(content, "utf-8", errors="replace")
        log.info("已保存原始页面：%s", path)

    # -- 搜索 --------------------------------------------------------------
    def _mode_order(self) -> list[str]:
        order: list[str] = []
        if self._learned_mode:
            order.append(self._learned_mode)
        if self.mode in ("html", "api"):
            order.append(self.mode)
        else:  # auto
            order.extend(["html", "api"])
        unique: list[str] = []
        for mode in order:
            if mode not in unique:
                unique.append(mode)
        return unique

    def search(self, watch: Watch, page_token: str = "") -> SearchResult:
        last_error: Exception | None = None
        for mode in self._mode_order():
            try:
                if mode == "api":
                    return self._search_api(watch, page_token)
                return self._search_html(watch, page_token)
            except Exception as exc:  # noqa: BLE001
                log.warning("搜索通道 %s 失败（%s）：%s", mode, watch.name, exc)
                last_error = exc
        raise HttpError(f"所有搜索通道都失败：{last_error}") from last_error

    def _search_html(self, watch: Watch, page_token: str) -> SearchResult:
        _, order = SORT_MAP.get(watch.sort, SORT_MAP["created_time"])
        params = {
            "keyword": normalize_keyword(watch.keyword),
            "status": "on_sale",
            "sort": "created_time" if watch.sort == "created_time" else "price",
            "order": "desc" if order == "ORDER_DESC" else "asc",
        }
        if page_token:
            params["page_token"] = page_token
        url = f"{SEARCH_WEB}?{urllib.parse.urlencode(params)}"
        resp = self.http.request(
            "GET",
            url,
            headers={
                "Referer": "https://jp.mercari.com/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        html = resp.text
        self._dump(f"search_{watch.name}", html)
        items = parse_items_from_html(html)
        token = parse_next_token_from_html(html) if page_token == "" else ""
        if not items:
            log.warning("搜索页没有解析出商品（%d 字节），稍后用 doctor --dump 复查", len(html))
        self._learned_mode = "html"
        return SearchResult(items=items, next_token=token, mode="html", raw_len=len(html))

    def _search_api(self, watch: Watch, page_token: str) -> SearchResult:
        sort, order = SORT_MAP.get(watch.sort, SORT_MAP["created_time"])
        payload = {
            "userId": "",
            "pageSize": 100,
            "pageToken": page_token or "",
            "searchSessionId": __import__("uuid").uuid4().hex,
            "searchCondition": {
                "keyword": normalize_keyword(watch.keyword),
                "excludeKeyword": "",
                "sort": sort,
                "order": order,
                "status": ["STATUS_ON_SALE"],
                "sizeId": [],
                "categoryId": [],
                "brandId": [],
                "sellerId": [],
                "priceMin": watch.filters.price_min or 0,
                "priceMax": watch.filters.price_max or 0,
                "itemConditionId": [],
                "shippingPayerIds": [],
                "shippingFromArea": [],
                "shippingMethodIds": [],
                "colorId": [],
                "hasDiscount": False,
                "isNoPrice": False,
            },
        }
        headers = {
            "DPoP": crypto.make_dpop(self._private_key(), SEARCH_API, "POST"),
            "X-Platform": "web",
            "X-Client-Version": "8c6d0dc6",
            "Origin": "https://jp.mercari.com",
            "Referer": "https://jp.mercari.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        resp = self.http.post_json(SEARCH_API, payload, headers=headers)
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            self._dump(f"api_{watch.name}", resp.text)
            raise HttpError(f"接口返回不是 JSON：{resp.text[:200]!r}") from exc
        items = [it for it in (normalize_item(raw) for raw in data.get("items", [])) if it]
        token = (data.get("meta") or {}).get("nextPageToken", "") or ""
        self._learned_mode = "api"
        return SearchResult(items=items, next_token=token, mode="api", raw_len=len(resp.body))

    # -- 单个商品 ----------------------------------------------------------
    def fetch_item(self, item_id: str) -> Item | None:
        """返回最新状态；商品已删除返回 None。"""
        url = ITEM_WEB.format(item_id=item_id)
        try:
            resp = self.http.request("GET", url, headers={"Referer": "https://jp.mercari.com/"})
        except HttpError as exc:
            if exc.status in (404, 410):
                return None
            raise
        html = resp.text
        self._dump(f"item_{item_id}", html)
        status = parse_status_from_item_page(html)
        item = Item(id=item_id, name="", price=None, status=status, url=item_url(item_id))
        name_match = re.search(r'"name"\s*:\s*"((?:[^"\\]|\\.){1,300})"', html)
        if name_match:
            item.name = name_match.group(1).replace('\\"', '"')
        price_match = re.search(r'"price"\s*:\s*"?(\d{1,9})"?', html)
        if price_match:
            item.price = _as_price(price_match.group(1))
        return item
