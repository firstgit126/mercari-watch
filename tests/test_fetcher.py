import unittest

from mercari_watch.fetcher import (
    normalize_keyword,
    parse_items_from_html,
    parse_next_token_from_html,
    parse_status_from_item_page,
)

NEXT_DATA_HTML = """<!doctype html><html><head></head><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"items":[
  {"id":"m11111111111","name":"Nintendo Switch 有機EL 本体","price":29800,
   "status":"ITEM_STATUS_ON_SALE","thumbnails":["https://static.mercdn.net/1.jpg"],
   "sellerId":"12345","created":1700000000},
  {"id":"m22222222222","name":"PS5 ディスクドライブ版","price":45000,
   "status":"ITEM_STATUS_ON_SALE","thumbnails":[]}
],"meta":{"nextPageToken":"tok123"}}}}
</script>
</body></html>"""

RSC_HTML = (
    "<html><body><script>self.__next_f.push([1,\"7:[\\\"$\\\",\\\"div\\\",null,{\\\"items\\\":"
    "[{\\\"id\\\":\\\"m33333333333\\\",\\\"name\\\":\\\"AirPods Pro\\\",\\\"price\\\":9800}]}]\")</script>"
    "</body></html>"
)

FALLBACK_HTML = (
    '<html><body><div data-x=\'{"id":"m44444444444","name":"Kindle Paperwhite",'
    '"price":7800,"status":"ITEM_STATUS_ON_SALE"}\'></div></body></html>'
)

ITEM_PAGE = (
    '<html><body><script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"item":{"id":"m11111111111","name":"Switch",'
    '"price":29800,"status":"ITEM_STATUS_SOLD_OUT"}}}}</script></body></html>'
)


class KeywordTests(unittest.TestCase):
    def test_full_width_space_normalized(self):
        # 日文输入法打出来的是 U+3000
        self.assertEqual(
            normalize_keyword("ロックマン\u3000ガレージキット"),
            "ロックマン ガレージキット",
        )

    def test_multiple_spaces_collapsed(self):
        self.assertEqual(normalize_keyword("  ロックマン   キット  "), "ロックマン キット")

    def test_half_width_untouched(self):
        self.assertEqual(normalize_keyword("PS5 ディスク"), "PS5 ディスク")


class ParseSearchPageTests(unittest.TestCase):
    def test_next_data_items(self):
        items = parse_items_from_html(NEXT_DATA_HTML)
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first.id, "m11111111111")
        self.assertEqual(first.name, "Nintendo Switch 有機EL 本体")
        self.assertEqual(first.price, 29800)
        self.assertEqual(first.status, "on_sale")
        self.assertTrue(first.url.endswith("/item/m11111111111"))
        self.assertEqual(first.thumb, "https://static.mercdn.net/1.jpg")

    def test_next_data_token(self):
        self.assertEqual(parse_next_token_from_html(NEXT_DATA_HTML), "tok123")

    def test_rsc_payload_items(self):
        items = parse_items_from_html(RSC_HTML)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, "m33333333333")
        self.assertEqual(items[0].price, 9800)

    def test_regex_fallback(self):
        items = parse_items_from_html(FALLBACK_HTML)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, "m44444444444")
        self.assertEqual(items[0].name, "Kindle Paperwhite")
        self.assertEqual(items[0].price, 7800)

    def test_empty_page_returns_nothing(self):
        self.assertEqual(parse_items_from_html("<html><body>no data</body></html>"), [])

    def test_duplicates_collapse(self):
        html = NEXT_DATA_HTML.replace("</body>", NEXT_DATA_HTML.split("<body>")[1])
        items = parse_items_from_html(html)
        self.assertEqual([i.id for i in items].count("m11111111111"), 1)


class ItemPageStatusTests(unittest.TestCase):
    def test_sold_out(self):
        self.assertEqual(parse_status_from_item_page(ITEM_PAGE), "sold_out")

    def test_on_sale_page(self):
        html = ITEM_PAGE.replace("ITEM_STATUS_SOLD_OUT", "ITEM_STATUS_ON_SALE")
        self.assertEqual(parse_status_from_item_page(html), "on_sale")

    def test_unknown(self):
        self.assertEqual(parse_status_from_item_page("<html>nothing</html>"), "unknown")


if __name__ == "__main__":
    unittest.main()
