import contextlib
import os
import shutil
import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest import mock

from mercari_watch.config import ConfigError, load_config
from mercari_watch.models import Event, Item
from mercari_watch.store import Store

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.toml"
CI_EXAMPLE = ROOT / "config.ci.toml"

# 临时目录放在项目内。注意不能用 tempfile：它会以 0o700 建目录，
# 在受限的沙箱环境里反过来会被拒绝写入。
_TMP_BASE = Path(__file__).resolve().parent / "_tmp"
_TMP_BASE.mkdir(exist_ok=True)


@contextlib.contextmanager
def tmpdir():
    path = _TMP_BASE / f"t{uuid.uuid4().hex[:8]}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class ConfigTests(unittest.TestCase):
    def test_example_config_loads(self):
        cfg = load_config(EXAMPLE)
        self.assertEqual(len(cfg.watches), 2)
        l1, l2 = cfg.watches
        self.assertEqual(l1.mode, "new")
        self.assertEqual(l2.mode, "price")
        self.assertTrue(l2.rules.price_increase)
        self.assertEqual(l1.rules.price_drop, False)

    def test_defaults_inherited_by_watch(self):
        cfg = load_config(EXAMPLE)
        # L2 的 filters 只写了 exclude_keywords，require_keywords 应来自 defaults
        self.assertIn("ジャンク", cfg.watches[1].filters.exclude_keywords)
        self.assertEqual(cfg.watches[1].filters.require_keywords, [])

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config(ROOT / "nope.toml")

    def test_invalid_toml(self):
        with tmpdir() as tmp:
            path = tmp / "bad.toml"
            path.write_text("[[watch]\nkeyword=", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_watch_without_keyword(self):
        with tmpdir() as tmp:
            path = tmp / "c.toml"
            path.write_text('[[watch]]\nname = "x"\n', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_duplicate_names(self):
        with tmpdir() as tmp:
            path = tmp / "c.toml"
            path.write_text(
                '[[watch]]\nname="a"\nkeyword="x"\n[[watch]]\nname="a"\nkeyword="y"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_interval_too_small(self):
        with tmpdir() as tmp:
            path = tmp / "c.toml"
            path.write_text('[[watch]]\nname="a"\nkeyword="x"\ninterval=5\n', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_invalid_mode(self):
        with tmpdir() as tmp:
            path = tmp / "c.toml"
            path.write_text(
                '[[watch]]\nname="a"\nkeyword="x"\nmode="cheap"\n', encoding="utf-8"
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("mode", str(ctx.exception))

    def test_mode_defaults_to_new(self):
        with tmpdir() as tmp:
            path = tmp / "c.toml"
            path.write_text('[[watch]]\nname="a"\nkeyword="x"\n', encoding="utf-8")
            self.assertEqual(load_config(path).watches[0].mode, "new")


class EmailEnvTests(unittest.TestCase):
    """CI 里邮箱参数靠环境变量注入，公开仓库不留凭据。"""

    ENV = {
        "MERCARI_WATCH_SMTP_HOST": "smtp.qq.com",
        "MERCARI_WATCH_SMTP_PORT": "587",
        "MERCARI_WATCH_SMTP_SECURITY": "starttls",
        "MERCARI_WATCH_SMTP_USERNAME": "me@qq.com",
        "MERCARI_WATCH_SMTP_PASSWORD": "code",
        "MERCARI_WATCH_SMTP_FROM": "me@qq.com",
        "MERCARI_WATCH_SMTP_TO": "me@qq.com, other@163.com",
    }

    def test_ci_config_needs_secrets(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("MERCARI_WATCH_")}
        with mock.patch.dict(os.environ, clean, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load_config(CI_EXAMPLE)
            # 服务器/端口写在文件里，邮箱和授权码必须由 Secrets 提供
            self.assertIn("email.username", str(ctx.exception))

    def test_env_fills_email(self):
        with mock.patch.dict(os.environ, self.ENV, clear=False):
            cfg = load_config(CI_EXAMPLE)
        self.assertEqual(cfg.email.host, "smtp.qq.com")
        self.assertEqual(cfg.email.port, 587)
        self.assertEqual(cfg.email.security, "starttls")
        self.assertEqual(cfg.email.to_addrs, ["me@qq.com", "other@163.com"])

    def test_env_overrides_file_value(self):
        with mock.patch.dict(os.environ, self.ENV, clear=False):
            cfg = load_config(EXAMPLE)
        self.assertEqual(cfg.email.port, 587)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tmpdir()
        self.tmp = self._tmp.__enter__()
        self.store = Store(self.tmp / "state.db")

    def tearDown(self):
        self.store.close()
        self._tmp.__exit__(None, None, None)

    def make_item(self, price=29800, item_id="m12345678901") -> Item:
        return Item(id=item_id, name="Switch", price=price)

    def test_insert_and_read(self):
        self.store.upsert_item(self.make_item())
        row = self.store.get_item("m12345678901")
        self.assertEqual(row["price"], 29800)
        self.assertEqual(row["title"], "Switch")

    def test_update_keeps_min_price(self):
        self.store.upsert_item(self.make_item(29800))
        self.store.upsert_item(self.make_item(25000))
        row = self.store.get_item("m12345678901")
        self.assertEqual(row["price"], 25000)
        self.assertEqual(row["price_min"], 25000)
        self.store.upsert_item(self.make_item(31000))
        row = self.store.get_item("m12345678901")
        self.assertEqual(row["price"], 31000)
        self.assertEqual(row["price_min"], 25000)

    def test_subscribe_reports_first_time(self):
        self.store.upsert_item(self.make_item())
        self.assertTrue(self.store.subscribe("L1", "m12345678901"))
        self.assertFalse(self.store.subscribe("L1", "m12345678901"))

    def test_two_watches_keep_separate_price_baselines(self):
        self.store.upsert_item(self.make_item(30000))
        self.store.subscribe("L1", "m12345678901")
        self.store.subscribe("L2", "m12345678901")
        self.store.update_sub("L2", "m12345678901", price=25000)
        self.assertEqual(self.store.get_sub("L2", "m12345678901")["last_price"], 25000)
        self.assertEqual(self.store.get_sub("L1", "m12345678901")["last_price"], 30000)
        self.assertEqual(self.store.count_subs("L1"), 1)
        self.assertEqual(self.store.count_subs("L2"), 1)

    def test_locked_and_unlock(self):
        self.store.upsert_item(self.make_item())
        self.store.subscribe("L2", "m12345678901", locked=True)
        locked = self.store.locked_subs("L2")
        self.assertEqual(len(locked), 1)
        self.assertEqual(locked[0]["title"], "Switch")
        self.assertEqual(self.store.unlock("L2"), 1)
        self.assertEqual(self.store.locked_subs("L2"), [])

    def test_status_due_selection(self):
        self.store.upsert_item(self.make_item(price=100))
        self.store.subscribe("L1", "m12345678901")
        # 刚订阅时 last_check=0，任何时候都算到期
        self.assertEqual(len(self.store.subs_due_for_status("L1", 10, 0)), 1)
        self.store.update_sub("L1", "m12345678901", price=None, checked=True)
        self.assertEqual(self.store.subs_due_for_status("L1", 10, 3600), [])
        self.assertEqual(len(self.store.subs_due_for_status("L1", 10, -1)), 1)
        self.store.set_item_status("m12345678901", "sold_out")
        self.assertEqual(self.store.subs_due_for_status("L1", 10, -1), [])

    def test_events_pending_flow(self):
        self.store.add_event(
            Event(kind="new_item", watch="w", item_id="m1", title="t", url="u", price=100)
        )
        pending = self.store.pending_events()
        self.assertEqual(len(pending), 1)
        self.store.mark_events_notified([pending[0]["id"]])
        self.assertEqual(self.store.pending_events(), [])

    def test_baseline_flag(self):
        self.assertEqual(self.store.watch_state("w")["baseline_done"], 0)
        self.store.mark_baseline("w")
        self.assertEqual(self.store.watch_state("w")["baseline_done"], 1)


class MigrationTests(unittest.TestCase):
    def test_v1_schema_is_rebuilt(self):
        with tmpdir() as tmp:
            path = tmp / "old.db"
            conn = sqlite3.connect(str(path))
            conn.execute(
                "CREATE TABLE items (item_id TEXT PRIMARY KEY, watch TEXT, title TEXT, price INTEGER)"
            )
            conn.execute("INSERT INTO items VALUES ('m1', 'w', 'old', 100)")
            conn.commit()
            conn.close()

            store = Store(path)
            try:
                columns = {row[1] for row in store.conn.execute("PRAGMA table_info(items)")}
                self.assertNotIn("watch", columns)
                self.assertIsNone(store.get_item("m1"))  # 旧数据已清
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
