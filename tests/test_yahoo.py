"""雅虎日拍解析器的单元测试（不联网）。"""

import json
import unittest

from mercari_watch.models import STATUS_ON_SALE, STATUS_REMOVED, STATUS_SOLD
from mercari_watch.yahoo import parse_item_status, parse_items_from_html


def make_page(items: list[dict]) -> str:
    payload = {
        "props": {
            "pageProps": {
                "initialState": {
                    "search": {"items": items},
                }
            }
        }
    }
    return (
        "<html><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )


def raw_item(**overrides) -> dict:
    base = {
        "auctionId": "p1249254316",
        "title": "ロックマン ガレージキット 未塗装",
        "price": 25000,
        "buyNowPrice": 40000,
        "bidCount": 0,
        "startTime": "2026-10-22T08:18:06+09:00",
        "endTime": "2026-10-29T22:18:06+09:00",
        "imageUrl": "https://auc-pctr.c.yimg.jp/i/example.jpg",
        "seller": "seller01",
    }
    base.update(overrides)
    return base


class SearchParsingTests(unittest.TestCase):
    def test_parses_items(self):
        html = make_page([raw_item(), raw_item(auctionId="n1249066201", price=9800)])
        items = parse_items_from_html(html)
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first.id, "p1249254316")
        self.assertEqual(first.price, 25000)
        self.assertEqual(first.name, "ロックマン ガレージキット 未塗装")
        self.assertEqual(first.status, STATUS_ON_SALE)
        self.assertIn("/jp/auction/p1249254316", first.url)
        self.assertTrue(first.url.startswith("https://auctions.yahoo.co.jp/"))
        self.assertEqual(first.thumb, "https://auc-pctr.c.yimg.jp/i/example.jpg")
        self.assertIsNotNone(first.created)

    def test_dedupes_by_auction_id(self):
        html = make_page([raw_item(), raw_item(title="重复")])
        self.assertEqual(len(parse_items_from_html(html)), 1)

    def test_missing_next_data_returns_empty(self):
        self.assertEqual(parse_items_from_html("<html><body>nope</body></html>"), [])

    def test_broken_json_returns_empty(self):
        html = '<html><script id="__NEXT_DATA__">{broken</script></html>'
        self.assertEqual(parse_items_from_html(html), [])

    def test_zero_price_becomes_none(self):
        items = parse_items_from_html(make_page([raw_item(price=0)]))
        self.assertIsNone(items[0].price)

    def test_string_price_is_parsed(self):
        items = parse_items_from_html(make_page([raw_item(price="12,500円")]))
        self.assertEqual(items[0].price, 12500)


class ItemStatusTests(unittest.TestCase):
    def test_active_auction(self):
        status, price, title = parse_item_status(make_page([raw_item()]), "p1249254316")
        self.assertEqual(status, STATUS_ON_SALE)
        self.assertEqual(price, 25000)
        self.assertIn("ロックマン", title)

    def test_ended_with_bid_is_sold(self):
        page = make_page([raw_item(endTime="2020-01-01T00:00:00+09:00", bidCount=3)])
        status, _, _ = parse_item_status(page, "p1249254316")
        self.assertEqual(status, STATUS_SOLD)

    def test_ended_without_bid_is_removed(self):
        page = make_page([raw_item(endTime="2020-01-01T00:00:00+09:00", bidCount=0)])
        status, _, _ = parse_item_status(page, "p1249254316")
        self.assertEqual(status, STATUS_REMOVED)

    def test_no_data_is_unknown(self):
        status, _, _ = parse_item_status("<html></html>", "x1")
        self.assertEqual(status, "unknown")


if __name__ == "__main__":
    unittest.main()
