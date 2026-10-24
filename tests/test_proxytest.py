import unittest

from mercari_watch.proxytest import mask_proxy, parse_proxy, verdict


def base_result(**overrides) -> dict:
    result = {
        "proxy": "http://1.2.3.4:8080",
        "parsed": {"host": "1.2.3.4", "port": 8080, "username": "", "has_password": False, "scheme": "http"},
        "tcp_ok": True,
        "tcp_ms": 30,
        "exit_ip": "203.0.113.9",
        "country": "JP",
        "http_status": 200,
        "bytes": 123456,
        "items": 12,
        "elapsed": 1.2,
        "error": "",
        "stage": "mercari",
    }
    result.update(overrides)
    return result


class ParseProxyTests(unittest.TestCase):
    def test_plain(self):
        parsed = parse_proxy("http://1.2.3.4:8080")
        self.assertEqual(parsed["host"], "1.2.3.4")
        self.assertEqual(parsed["port"], 8080)
        self.assertEqual(parsed["username"], "")

    def test_with_auth(self):
        parsed = parse_proxy("http://user:p%40ss@1.2.3.4:8080")
        self.assertEqual(parsed["username"], "user")
        self.assertTrue(parsed["has_password"])

    def test_default_ports(self):
        self.assertEqual(parse_proxy("http://1.2.3.4")["port"], 80)
        self.assertEqual(parse_proxy("https://1.2.3.4")["port"], 443)

    def test_missing_scheme(self):
        with self.assertRaises(ValueError):
            parse_proxy("1.2.3.4:8080")

    def test_socks5_rejected_with_hint(self):
        with self.assertRaises(ValueError) as ctx:
            parse_proxy("socks5://1.2.3.4:1080")
        self.assertIn("SOCKS5", str(ctx.exception))


class MaskTests(unittest.TestCase):
    def test_password_hidden(self):
        self.assertEqual(mask_proxy("http://u:secret@1.2.3.4:8080"), "http://u:***@1.2.3.4:8080")

    def test_no_auth(self):
        self.assertEqual(mask_proxy("http://1.2.3.4:8080"), "http://1.2.3.4:8080")


class VerdictTests(unittest.TestCase):
    def test_good_proxy(self):
        level, lines = verdict(base_result())
        self.assertEqual(level, "ok")
        self.assertTrue(any("12 个商品" in line for line in lines))

    def test_tcp_failure(self):
        level, lines = verdict(base_result(tcp_ok=False, error="拒绝连接", stage="tcp"))
        self.assertEqual(level, "fail")
        self.assertTrue(any("拒绝连接" in line for line in lines))

    def test_403_is_fail(self):
        level, _ = verdict(base_result(http_status=403, items=0, error="煤炉返回 HTTP 403"))
        self.assertEqual(level, "fail")

    def test_200_without_items_is_warn(self):
        level, _ = verdict(base_result(items=0))
        self.assertEqual(level, "warn")

    def test_foreign_country_warns(self):
        level, lines = verdict(base_result(country="DE"))
        self.assertEqual(level, "ok")  # 仍可用，但要提示
        self.assertTrue(any("日本/亚太" in line for line in lines))

    def test_bad_scheme(self):
        level, lines = verdict(base_result(stage="parse", error="暂不支持"))
        self.assertEqual(level, "fail")


if __name__ == "__main__":
    unittest.main()
