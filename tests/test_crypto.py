import base64
import unittest

from mercari_watch import crypto


class CryptoTests(unittest.TestCase):
    def test_self_test_passes(self):
        ok, detail = crypto.self_test()
        self.assertTrue(ok, detail)

    def test_sign_verify_roundtrip(self):
        priv = crypto.new_private_key()
        pub = crypto.public_key(priv)
        msg = b"hello mercari"
        sig = crypto.sign_es256(priv, msg)
        self.assertTrue(crypto.verify_es256(pub, msg, sig))
        self.assertFalse(crypto.verify_es256(pub, b"hello mercarj", sig))

    def test_signature_is_deterministic(self):
        priv = crypto.new_private_key()
        sig1 = crypto.sign_es256(priv, b"same")
        sig2 = crypto.sign_es256(priv, b"same")
        self.assertEqual(sig1, sig2)

    def test_public_key_on_curve(self):
        priv = crypto.new_private_key()
        x, y = crypto.public_key(priv)
        left = (y * y) % crypto.P
        right = (x * x * x + crypto.A * x + crypto.B) % crypto.P
        self.assertEqual(left, right)

    def test_dpop_signature_is_raw_not_der(self):
        """JWS 的 ES256 用 64 字节 R||S，不是 DER；送 DER 会被服务端判 invalid token。"""
        priv = crypto.new_private_key()
        token = crypto.make_dpop(priv, "https://api.mercari.jp/v2/entities:search", "POST")
        sig = token.split(".")[2]
        raw = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        self.assertEqual(len(raw), 64)

    def test_dpop_token_shape(self):
        priv = crypto.new_private_key()
        token = crypto.make_dpop(priv, "https://api.mercari.jp/v2/entities:search", "POST")
        header, payload, signature = token.split(".")
        self.assertTrue(header and payload and signature)
        import base64
        import json

        def pad(s):
            return s + "=" * (-len(s) % 4)

        head = json.loads(base64.urlsafe_b64decode(pad(header)))
        self.assertEqual(head["alg"], "ES256")
        self.assertEqual(head["typ"], "dpop+jwt")
        self.assertEqual(head["jwk"]["crv"], "P-256")
        claims = json.loads(base64.urlsafe_b64decode(pad(payload)))
        self.assertEqual(claims["htm"], "POST")
        self.assertEqual(claims["htu"], "https://api.mercari.jp/v2/entities:search")
        self.assertIn("jti", claims)
        self.assertIn("iat", claims)


if __name__ == "__main__":
    unittest.main()
