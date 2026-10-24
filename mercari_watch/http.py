"""极简 HTTP 客户端：代理、重试、gzip、cookie，全部来自标准库。"""

from __future__ import annotations

import gzip
import http.cookiejar
import logging
import random
import ssl
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass

log = logging.getLogger(__name__)


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None, body: bytes = b"") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes
    url: str

    @property
    def text(self) -> str:
        charset = "utf-8"
        ctype = self.headers.get("content-type", "")
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self):
        import json

        return json.loads(self.text)


class HttpClient:
    def __init__(
        self,
        *,
        proxy: str = "",
        timeout: int = 20,
        retries: int = 2,
        user_agent: str = "Mozilla/5.0",
    ) -> None:
        self.timeout = timeout
        self.retries = max(0, retries)
        self.user_agent = user_agent
        self.cookies = http.cookiejar.CookieJar()
        handlers: list = [urllib.request.HTTPCookieProcessor(self.cookies)]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            log.info("使用代理：%s", proxy)
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        context = ssl.create_default_context()
        handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    # -- 内部 --------------------------------------------------------------
    def _decode(self, resp) -> bytes:
        body = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if encoding == "gzip":
            try:
                body = gzip.decompress(body)
            except OSError:
                pass
        elif encoding == "deflate":
            try:
                body = zlib.decompress(body)
            except zlib.error:
                pass
        return body

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        body: bytes | None = None,
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    ) -> Response:
        merged = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Language": "ja-JP,ja;q=0.9,zh-CN;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if headers:
            merged.update(headers)

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=body, headers=merged, method=method.upper())
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    return Response(
                        status=resp.status,
                        headers=dict(resp.headers.items()),
                        body=self._decode(resp),
                        url=resp.geturl(),
                    )
            except urllib.error.HTTPError as exc:
                payload = b""
                try:
                    payload = exc.read() or b""
                except Exception:  # noqa: BLE001
                    pass
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    last_error = HttpError(f"HTTP {exc.code}", exc.code, payload)
                    self._backoff(attempt, exc.code)
                    continue
                raise HttpError(f"HTTP {exc.code}", exc.code, payload) from exc
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
                last_error = exc
                if attempt < self.retries:
                    self._backoff(attempt, None)
                    continue
                raise HttpError(f"网络错误：{exc}") from exc
        raise HttpError(f"请求失败：{last_error}") from last_error

    def _backoff(self, attempt: int, status: int | None) -> None:
        delay = min(30.0, (2.0**attempt)) + random.uniform(0, 1.5)
        if status == 429:
            delay = max(delay, 10.0)
        log.warning("请求失败，%.1f 秒后重试", delay)
        time.sleep(delay)

    # -- 便捷方法 ----------------------------------------------------------
    def get_text(self, url: str, *, headers: dict | None = None) -> Response:
        return self.request("GET", url, headers=headers)

    def post_json(self, url: str, payload, *, headers: dict | None = None) -> Response:
        import json

        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        merged = {"Content-Type": "application/json", "Accept": "application/json, text/plain, */*"}
        if headers:
            merged.update(headers)
        return self.request("POST", url, headers=merged, body=data, accept=merged["Accept"])
