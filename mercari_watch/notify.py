"""通知：SMTP 邮件（HTML 摘要）+ 控制台输出。"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
import time
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import Email
from .models import Event

log = logging.getLogger(__name__)

_KIND_COLOR = {
    "new_item": "#0969da",
    "price_drop": "#1a7f37",
    "price_up": "#cf222e",
    "below_price": "#bc4c00",
    "sold_out": "#8250df",
    "removed": "#6e7781",
    "back_on_sale": "#0a7ea4",
    "locked": "#0a3069",
}


class NotifyError(Exception):
    pass


def render_html(events: list[Event], prefix: str) -> str:
    rows = []
    for ev in events:
        color = _KIND_COLOR.get(ev.kind, "#444")
        price = f"¥{ev.price:,}" if ev.price else "—"
        rows.append(
            f"""<tr>
<td style="padding:10px 12px;border-bottom:1px solid #eaeef2;white-space:nowrap;">
  <span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;
               color:#fff;background:{color};">{html.escape(ev.kind_label)}</span>
</td>
<td style="padding:10px 12px;border-bottom:1px solid #eaeef2;">
  <a href="{html.escape(ev.url)}" style="color:#0969da;text-decoration:none;font-weight:600;">
    {html.escape(ev.title or ev.item_id)}
  </a>
  <div style="color:#57606a;font-size:12px;margin-top:4px;">
    {html.escape(ev.detail)} ｜ 监控项：{html.escape(ev.watch)} ｜ ID：{ev.item_id}
  </div>
</td>
<td style="padding:10px 12px;border-bottom:1px solid #eaeef2;text-align:right;
           white-space:nowrap;font-weight:600;">{price}</td>
</tr>"""
        )
    body = f"""<!doctype html>
<html><body style="margin:0;padding:16px;background:#f6f8fa;
      font-family:-apple-system,'Segoe UI','Hiragino Sans','Microsoft YaHei',sans-serif;">
  <div style="max-width:680px;margin:0 auto;background:#fff;border:1px solid #d0d7de;border-radius:10px;overflow:hidden;">
    <div style="padding:16px 20px;border-bottom:1px solid #d0d7de;background:#f6f8fa;">
      <div style="font-size:16px;font-weight:700;">{html.escape(prefix)}　发现 {len(events)} 条变化</div>
      <div style="color:#57606a;font-size:12px;margin-top:4px;">{time.strftime('%Y-%m-%d %H:%M:%S')}</div>
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">{''.join(rows)}</table>
    <div style="padding:12px 20px;color:#8b949e;font-size:12px;">
      由 mercari-watch 自动发送
    </div>
  </div>
</body></html>"""
    return body


def render_text(events: list[Event], prefix: str) -> str:
    lines = [f"{prefix} 发现 {len(events)} 条变化", time.strftime("%Y-%m-%d %H:%M:%S"), ""]
    for ev in events:
        price = f"¥{ev.price:,}" if ev.price else "—"
        lines.append(f"[{ev.kind_label}] {ev.title or ev.item_id} {price}")
        lines.append(f"  {ev.detail}")
        lines.append(f"  {ev.url}")
    return "\n".join(lines)


class EmailNotifier:
    def __init__(self, cfg: Email) -> None:
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def _connect(self):
        cfg = self.cfg
        if cfg.security == "ssl":
            context = ssl.create_default_context()
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
            server.ehlo()
            if cfg.security == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
        if cfg.username:
            server.login(cfg.username, cfg.password)
        return server

    def send_events(self, events: list[Event]) -> None:
        if not events:
            return
        prefix = self.cfg.subject_prefix
        subject = f"{prefix} {len(events)} 条变化"
        self._send(subject, render_text(events, prefix), render_html(events, prefix))

    def send_test(self) -> None:
        prefix = self.cfg.subject_prefix
        demo = [
            Event(
                kind="new_item",
                watch="测试关键字",
                item_id="m00000000001",
                title="（测试邮件）Nintendo Switch 有機EL 本体",
                url="https://jp.mercari.com/item/m00000000001",
                price=29800,
                detail="这是一封配置测试邮件，收到即代表通知通道正常",
            )
        ]
        self._send(f"{prefix} 配置测试", render_text(demo, prefix), render_html(demo, prefix))

    def _send(self, subject: str, text: str, html_body: str) -> None:
        cfg = self.cfg
        msg = EmailMessage()
        # 新版 EmailMessage 只接受字符串 / Address，不能塞 Header 对象
        msg["Subject"] = subject
        msg["From"] = Address(display_name="煤炉监控", addr_spec=cfg.from_addr)
        msg["To"] = ", ".join(cfg.to_addrs)
        msg["Date"] = formatdate(localtime=True)
        # 给邮件一个唯一 ID：万一还是收到重复的，邮件客户端能按它去重
        msg["Message-ID"] = make_msgid(domain=cfg.from_addr.split("@")[-1] or "mercari-watch")
        msg.set_content(text, charset="utf-8")
        msg.add_alternative(html_body, subtype="html", charset="utf-8")

        # 连接 / 登录阶段可以重试：这一步失败说明邮件根本没发出去，重试是安全的。
        last_error: Exception | None = None
        server = None
        for attempt in range(3):
            try:
                server = self._connect()
                break
            except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
                last_error = exc
                log.warning("连接邮件服务器失败（第 %d 次）：%s", attempt + 1, exc)
                time.sleep(3 * (attempt + 1))
        if server is None:
            raise NotifyError(f"连接邮件服务器失败：{last_error}")

        # 发送阶段只做一次，绝不重试。
        # 原因：服务器可能已经收下邮件、只是在读回执时断线；重试就会导致同一封信发两遍
        # （实测踩过：两封邮件连时间戳都一模一样）。
        try:
            server.send_message(msg)
            log.info("邮件已发送：%s -> %s", subject, ", ".join(cfg.to_addrs))
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise NotifyError(
                f"发送中断：{exc}（邮件可能已经投递成功，为避免重复不再重试）"
            ) from exc
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
