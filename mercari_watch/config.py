"""配置加载与校验（TOML，无第三方依赖）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 老版本回退
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


class ConfigError(Exception):
    """配置不合法时抛出，消息直接给用户看。"""


@dataclass
class Filters:
    price_min: int = 0            # 0 = 不限
    price_max: int = 0            # 0 = 不限
    exclude_keywords: list[str] = field(default_factory=list)
    require_keywords: list[str] = field(default_factory=list)

    def matches(self, title: str, price: int | None) -> bool:
        low = title.lower()
        if self.price_min and price is not None and price < self.price_min:
            return False
        if self.price_max and price is not None and price > self.price_max:
            return False
        for word in self.require_keywords:
            if word.lower() not in low:
                return False
        for word in self.exclude_keywords:
            if word and word.lower() in low:
                return False
        return True


@dataclass
class Rules:
    new_item: bool = True             # 新上架就提醒
    price_drop: bool = True           # 降价提醒
    price_increase: bool = False      # 涨价也提醒（L2 常用）
    price_drop_percent: float = 5.0   # 降幅达到该百分比才提醒（0 = 不按比例）
    price_drop_absolute: int = 0      # 或降价金额达到该值（0 = 不按金额）
    below_price: int = 0              # 跌破该价格时额外提醒（0 = 关闭）
    status_change: bool = True        # 售出/下架提醒
    alert_on_first_run: bool = False  # 首次建立基线时是否也提醒老商品


@dataclass
class Watch:
    name: str
    keyword: str
    # mercari = 煤炉；yahoo = 雅虎日拍（ヤフオク）
    site: str = "mercari"
    # new   = L1：每轮搜索，发现新商品就报
    # price = L2：先搜索锁定一件（item_id），之后只盯这一件的价格和状态
    mode: str = "new"
    interval: int = 300
    pages: int = 1
    sort: str = "created_time"     # created_time | price_asc | price_desc | score
    enabled: bool = True
    filters: Filters = field(default_factory=Filters)
    rules: Rules = field(default_factory=Rules)


@dataclass
class Network:
    proxy: str = ""
    timeout: int = 20
    retries: int = 2
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    fetch_mode: str = "auto"       # auto | html | api
    ca_bundle: str = ""            # 额外的 CA 证书文件，补系统信任库的缺口
    dump_html: bool = False        # 调试用：保存原始页面
    status_poll_batch: int = 20    # 每轮检查多少个在售商品的状态
    status_poll_interval: int = 900  # 同一商品多久复查一次状态（秒）


@dataclass
class Email:
    enabled: bool = False
    host: str = ""
    port: int = 465
    security: str = "ssl"          # ssl | starttls | plain
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    subject_prefix: str = "[煤炉监控]"

    def problems(self) -> list[str]:
        missing = []
        if not self.host:
            missing.append("notify.email.host")
        if not self.username:
            missing.append("notify.email.username")
        if not self.password:
            missing.append("notify.email.password")
        if not self.from_addr:
            missing.append("notify.email.from_addr")
        if not self.to_addrs:
            missing.append("notify.email.to_addrs")
        return missing


@dataclass
class Config:
    path: Path
    app_dir: Path
    data_dir: Path
    log_level: str
    network: Network
    email: Email
    watches: list[Watch]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "watch.log"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "dpop_key.json"


# ---- 解析辅助 --------------------------------------------------------------
def _as_int(raw: Any, name: str) -> int:
    if isinstance(raw, bool):
        raise ConfigError(f"{name} 需要整数，却拿到布尔值")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and float(raw).is_integer():
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.replace(",", "").replace("_", "").strip())
        except ValueError as exc:
            raise ConfigError(f"{name} 需要整数，却拿到 {raw!r}") from exc
    raise ConfigError(f"{name} 需要整数，却拿到 {raw!r}")


def _as_float(raw: Any, name: str) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError as exc:
            raise ConfigError(f"{name} 需要数字，却拿到 {raw!r}") from exc
    raise ConfigError(f"{name} 需要数字，却拿到 {raw!r}")


def _as_bool(raw: Any, name: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
    if isinstance(raw, int):
        return bool(raw)
    raise ConfigError(f"{name} 需要 true/false，却拿到 {raw!r}")


def _as_str(raw: Any, name: str) -> str:
    if isinstance(raw, str):
        return raw
    raise ConfigError(f"{name} 需要字符串，却拿到 {raw!r}")


def _as_str_list(raw: Any, name: str) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        out = []
        for entry in raw:
            if not isinstance(entry, str):
                raise ConfigError(f"{name} 里只能写字符串，却拿到 {entry!r}")
            if entry.strip():
                out.append(entry.strip())
        return out
    raise ConfigError(f"{name} 需要字符串数组，却拿到 {raw!r}")


def _merge_dict(base: dict, override: dict) -> dict:
    out = dict(base)
    out.update(override)
    return out


def _load_filters(raw: dict | None, base: Filters) -> Filters:
    data = _merge_dict(_filters_to_dict(base), raw or {})
    return Filters(
        price_min=_as_int(data.get("price_min", 0), "filters.price_min"),
        price_max=_as_int(data.get("price_max", 0), "filters.price_max"),
        exclude_keywords=_as_str_list(data.get("exclude_keywords"), "filters.exclude_keywords"),
        require_keywords=_as_str_list(data.get("require_keywords"), "filters.require_keywords"),
    )


def _load_rules(raw: dict | None, base: Rules) -> Rules:
    data = _merge_dict(_rules_to_dict(base), raw or {})
    return Rules(
        new_item=_as_bool(data.get("new_item", True), "rules.new_item"),
        price_drop=_as_bool(data.get("price_drop", True), "rules.price_drop"),
        price_increase=_as_bool(data.get("price_increase", False), "rules.price_increase"),
        price_drop_percent=_as_float(data.get("price_drop_percent", 0), "rules.price_drop_percent"),
        price_drop_absolute=_as_int(data.get("price_drop_absolute", 0), "rules.price_drop_absolute"),
        below_price=_as_int(data.get("below_price", 0), "rules.below_price"),
        status_change=_as_bool(data.get("status_change", True), "rules.status_change"),
        alert_on_first_run=_as_bool(
            data.get("alert_on_first_run", False), "rules.alert_on_first_run"
        ),
    )


def _filters_to_dict(f: Filters) -> dict:
    return {
        "price_min": f.price_min,
        "price_max": f.price_max,
        "exclude_keywords": list(f.exclude_keywords),
        "require_keywords": list(f.require_keywords),
    }


def _rules_to_dict(r: Rules) -> dict:
    return {
        "new_item": r.new_item,
        "price_drop": r.price_drop,
        "price_increase": r.price_increase,
        "price_drop_percent": r.price_drop_percent,
        "price_drop_absolute": r.price_drop_absolute,
        "below_price": r.below_price,
        "status_change": r.status_change,
        "alert_on_first_run": r.alert_on_first_run,
    }


def _decode_config(raw: bytes) -> str:
    """容忍记事本常见的 BOM（UTF-8 / UTF-16），否则按 UTF-8 严格解码。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8")
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le")
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            "配置文件不是 UTF-8 编码，请用记事本/VS Code 另存为 UTF-8 再试"
        ) from exc


# 环境变量覆盖：CI（GitHub Actions）里用它注入邮箱密码，
# 这样公开仓库里的 config 文件就不用写任何凭据。
_EMAIL_ENV = {
    "host": "MERCARI_WATCH_SMTP_HOST",
    "port": "MERCARI_WATCH_SMTP_PORT",
    "security": "MERCARI_WATCH_SMTP_SECURITY",
    "username": "MERCARI_WATCH_SMTP_USERNAME",
    "password": "MERCARI_WATCH_SMTP_PASSWORD",
    "from_addr": "MERCARI_WATCH_SMTP_FROM",
    "to_addrs": "MERCARI_WATCH_SMTP_TO",
}


def _apply_email_env(email: Email) -> Email:
    for field_name, env_name in _EMAIL_ENV.items():
        raw_value = os.environ.get(env_name)
        if raw_value is None or not raw_value.strip():
            continue
        value = raw_value.strip()
        if field_name == "port":
            email.port = _as_int(value, env_name)
        elif field_name == "to_addrs":
            email.to_addrs = [part.strip() for part in value.split(",") if part.strip()]
        else:
            setattr(email, field_name, value)
    if not email.from_addr and email.username:
        email.from_addr = email.username
    if email.security not in {"ssl", "starttls", "plain"}:
        raise ConfigError("notify.email.security 只能是 ssl / starttls / plain")
    return email


def load_config(path: str | Path) -> Config:
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.exists():
        raise ConfigError(f"配置文件不存在：{cfg_path}")
    if tomllib is None:
        raise ConfigError("当前 Python 缺少 tomllib（需要 3.11+，或 pip install tomli）")

    try:
        raw = tomllib.loads(_decode_config(cfg_path.read_bytes()))
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"配置文件解析失败：{exc}") from exc

    app_dir = cfg_path.parent
    app_raw = raw.get("app", {}) or {}
    data_dir = Path(_as_str(app_raw.get("data_dir", "data"), "app.data_dir"))
    if not data_dir.is_absolute():
        data_dir = app_dir / data_dir
    log_level = _as_str(app_raw.get("log_level", "INFO"), "app.log_level").upper()

    net_raw = raw.get("network", {}) or {}
    ca_bundle_raw = _as_str(net_raw.get("ca_bundle", "") or "", "network.ca_bundle").strip()
    ca_bundle = ""
    if ca_bundle_raw:
        candidate = Path(ca_bundle_raw)
        ca_bundle = str(candidate if candidate.is_absolute() else (app_dir / candidate))
    network = Network(
        proxy=_as_str(net_raw.get("proxy", "") or "", "network.proxy"),
        timeout=_as_int(net_raw.get("timeout", 20), "network.timeout"),
        retries=_as_int(net_raw.get("retries", 2), "network.retries"),
        user_agent=_as_str(net_raw.get("user_agent", Network.user_agent), "network.user_agent"),
        fetch_mode=_as_str(net_raw.get("fetch_mode", "auto"), "network.fetch_mode").lower(),
        ca_bundle=ca_bundle,
        dump_html=_as_bool(net_raw.get("dump_html", False), "network.dump_html"),
        status_poll_batch=_as_int(net_raw.get("status_poll_batch", 20), "network.status_poll_batch"),
        status_poll_interval=_as_int(
            net_raw.get("status_poll_interval", 900), "network.status_poll_interval"
        ),
    )
    if network.fetch_mode not in {"auto", "html", "api"}:
        raise ConfigError("network.fetch_mode 只能是 auto / html / api")
    if network.timeout < 5:
        raise ConfigError("network.timeout 至少 5 秒")

    notify_raw = raw.get("notify", {}) or {}
    email_raw = (notify_raw.get("email", {}) or {})
    password = _as_str(email_raw.get("password", "") or "", "notify.email.password")
    env_password = os.environ.get("MERCARI_WATCH_SMTP_PASSWORD")
    if not password and env_password:
        password = env_password
    email = Email(
        enabled=_as_bool(email_raw.get("enabled", False), "notify.email.enabled"),
        host=_as_str(email_raw.get("host", "") or "", "notify.email.host"),
        port=_as_int(email_raw.get("port", 465), "notify.email.port"),
        security=_as_str(email_raw.get("security", "ssl"), "notify.email.security").lower(),
        username=_as_str(email_raw.get("username", "") or "", "notify.email.username"),
        password=password,
        from_addr=_as_str(email_raw.get("from_addr", "") or "", "notify.email.from_addr"),
        to_addrs=_as_str_list(email_raw.get("to_addrs"), "notify.email.to_addrs"),
        subject_prefix=_as_str(
            notify_raw.get("subject_prefix", "[煤炉监控]"), "notify.subject_prefix"
        ),
    )
    if email.security not in {"ssl", "starttls", "plain"}:
        raise ConfigError("notify.email.security 只能是 ssl / starttls / plain")
    if not email.from_addr and email.username:
        email.from_addr = email.username
    email = _apply_email_env(email)
    if email.enabled and email.problems():
        raise ConfigError("邮件通知已启用但缺少配置：" + "、".join(email.problems()))

    defaults_raw = raw.get("defaults", {}) or {}
    base_filters = _load_filters(defaults_raw.get("filters"), Filters())
    base_rules = _load_rules(defaults_raw.get("rules"), Rules())

    watches_raw = raw.get("watch", []) or []
    if not isinstance(watches_raw, list):
        raise ConfigError("[[watch]] 必须是数组")
    watches: list[Watch] = []
    seen_names: set[str] = set()
    for idx, item in enumerate(watches_raw, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"第 {idx} 个 [[watch]] 不是表")
        keyword = _as_str(item.get("keyword", "") or "", f"watch[{idx}].keyword").strip()
        if not keyword:
            raise ConfigError(f"第 {idx} 个 [[watch]] 缺少 keyword")
        name = _as_str(item.get("name", "") or "", f"watch[{idx}].name").strip() or keyword
        if name in seen_names:
            raise ConfigError(f"[[watch]] 名称重复：{name}")
        seen_names.add(name)
        interval = _as_int(item.get("interval", 300), f"watch[{idx}].interval")
        if interval < 30:
            raise ConfigError(f"{name}：interval 至少 30 秒（避免过于频繁的请求）")
        pages = _as_int(item.get("pages", 1), f"watch[{idx}].pages")
        if pages < 1 or pages > 10:
            raise ConfigError(f"{name}：pages 只能是 1-10")
        sort = _as_str(item.get("sort", "created_time"), f"watch[{idx}].sort").lower()
        if sort not in {"created_time", "price_asc", "price_desc", "score"}:
            raise ConfigError(f"{name}：sort 只能是 created_time / price_asc / price_desc / score")
        mode = _as_str(item.get("mode", "new"), f"watch[{idx}].mode").lower()
        if mode not in {"new", "price"}:
            raise ConfigError(f"{name}：mode 只能是 new（L1 找新货）或 price（L2 盯价格）")
        if mode == "price" and pages < 1:
            raise ConfigError(f"{name}：mode=price 至少要 1 页用来锁定商品")
        site = _as_str(item.get("site", "mercari"), f"watch[{idx}].site").lower()
        if site not in {"mercari", "yahoo"}:
            raise ConfigError(f"{name}：site 只能是 mercari（煤炉）或 yahoo（雅虎日拍）")
        watches.append(
            Watch(
                name=name,
                keyword=keyword,
                site=site,
                mode=mode,
                interval=interval,
                pages=pages,
                sort=sort,
                enabled=_as_bool(item.get("enabled", True), f"watch[{idx}].enabled"),
                filters=_load_filters(item.get("filters"), base_filters),
                rules=_load_rules(item.get("rules"), base_rules),
            )
        )

    if not watches:
        raise ConfigError("至少需要配置一个 [[watch]]")

    return Config(
        path=cfg_path,
        app_dir=app_dir,
        data_dir=data_dir,
        log_level=log_level,
        network=network,
        email=email,
        watches=watches,
    )
