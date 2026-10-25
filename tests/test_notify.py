"""邮件发送的回归测试：重点是「发送绝不重试」。

踩过的坑：126 已经把邮件收下了，但读回执时连接断了，旧逻辑会重试，
结果同一封信发了两遍，连时间戳都一样。
"""

import smtplib
import unittest
from unittest import mock

from mercari_watch.config import Email
from mercari_watch.notify import EmailNotifier, NotifyError


def make_email() -> Email:
    return Email(
        enabled=True,
        host="smtp.example.com",
        port=465,
        security="ssl",
        username="a@example.com",
        password="secret",
        from_addr="a@example.com",
        to_addrs=["a@example.com"],
    )


class StubServer:
    """记录 send_message 调了几次；可以配置成发送时抛异常。"""

    def __init__(self, fail_send: bool = False):
        self.send_calls = 0
        self.quit_calls = 0
        self.fail_send = fail_send

    def send_message(self, msg):
        self.send_calls += 1
        if self.fail_send:
            # 模拟「服务器已收单、但读响应时断线」
            raise smtplib.SMTPServerDisconnected("connection lost")
        return {}

    def quit(self):
        self.quit_calls += 1


class SendOnceTests(unittest.TestCase):
    def test_send_failure_is_not_retried(self):
        notifier = EmailNotifier(make_email())
        server = StubServer(fail_send=True)
        with mock.patch.object(notifier, "_connect", return_value=server):
            with mock.patch("mercari_watch.notify.time.sleep"):
                with self.assertRaises(NotifyError):
                    notifier._send("t", "text", "<p>html</p>")
        self.assertEqual(server.send_calls, 1, "发送失败后绝不能重发，否则会重复投递")
        self.assertEqual(server.quit_calls, 1)

    def test_successful_send_happens_once(self):
        notifier = EmailNotifier(make_email())
        server = StubServer(fail_send=False)
        with mock.patch.object(notifier, "_connect", return_value=server):
            notifier._send("t", "text", "<p>html</p>")
        self.assertEqual(server.send_calls, 1)

    def test_connect_failure_retries_then_raises(self):
        notifier = EmailNotifier(make_email())
        attempts = {"n": 0}

        def boom():
            attempts["n"] += 1
            raise OSError("cannot connect")

        with mock.patch.object(notifier, "_connect", side_effect=boom):
            with mock.patch("mercari_watch.notify.time.sleep"):
                with self.assertRaises(NotifyError):
                    notifier._send("t", "text", "<p>html</p>")
        self.assertEqual(attempts["n"], 3, "连接阶段允许重试")

    def test_message_has_message_id(self):
        notifier = EmailNotifier(make_email())
        server = StubServer(fail_send=False)
        captured = {}

        def capture(msg):
            captured["msg"] = msg
            server.send_calls += 1
            return {}

        with mock.patch.object(server, "send_message", side_effect=capture):
            with mock.patch.object(notifier, "_connect", return_value=server):
                notifier._send("t", "text", "<p>html</p>")
        self.assertIn("Message-ID", captured["msg"])


if __name__ == "__main__":
    unittest.main()
