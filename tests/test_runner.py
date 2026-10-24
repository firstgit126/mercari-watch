"""用假客户端跑完整的 Runner 流程，验证 L1/L2 的行为。

这里不碰网络：FakeClient 直接返回预设的商品，重点验证「首轮建基线 / 第二轮报新增 /
L2 锁定 / 价格涨跌 / 售出」这些容易出错的状态迁移。
"""

import contextlib
import shutil
import unittest
import uuid
from pathlib import Path
from unittest import mock

from mercari_watch.config import Config, Email, Filters, Network, Rules, Watch
from mercari_watch.fetcher import SearchResult
from mercari_watch.models import STATUS_ON_SALE, STATUS_SOLD, Item
from mercari_watch.notify import EmailNotifier
from mercari_watch.runner import Runner
from mercari_watch.store import Store

_TMP_BASE = Path(__file__).resolve().parent / "_tmp"
_TMP_BASE.mkdir(exist_ok=True)


@contextlib.contextmanager
def tmpdir():
    path = _TMP_BASE / f"r{uuid.uuid4().hex[:8]}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeClient:
    """按需返回预设结果，并记录被调用的次数。"""

    def __init__(self, search_items=None, items=None):
        self.search_items = list(search_items or [])
        self.items = dict(items or {})
        self.search_calls = 0
        self.fetch_calls = 0

    def search(self, watch, page_token=""):
        self.search_calls += 1
        return SearchResult(items=[_copy(i) for i in self.search_items], next_token="", mode="html")

    def fetch_item(self, item_id):
        self.fetch_calls += 1
        found = self.items.get(item_id)
        return _copy(found) if found else None


def _copy(item: Item) -> Item:
    return Item(
        id=item.id,
        name=item.name,
        price=item.price,
        status=item.status,
        url=item.url,
        thumb=item.thumb,
        seller=item.seller,
        created=item.created,
    )


def make_item(item_id="m11111111111", price=30000, name="Switch", status=STATUS_ON_SALE) -> Item:
    return Item(id=item_id, name=name, price=price, status=status)


def make_config(data_dir: Path, watches: list[Watch]) -> Config:
    return Config(
        path=data_dir / "config.toml",
        app_dir=data_dir,
        data_dir=data_dir,
        log_level="INFO",
        network=Network(),
        email=Email(enabled=False),
        watches=watches,
    )


def make_l1(**rules_kwargs) -> Watch:
    base = dict(new_item=True, price_drop=False, price_increase=False,
                price_drop_percent=0, price_drop_absolute=0, below_price=0,
                status_change=False, alert_on_first_run=False)
    base.update(rules_kwargs)
    return Watch(name="L1", keyword="switch", mode="new", rules=Rules(**base))


def make_l2(**rules_kwargs) -> Watch:
    base = dict(new_item=False, price_drop=True, price_increase=True,
                price_drop_percent=0, price_drop_absolute=0, below_price=0,
                status_change=True, alert_on_first_run=False)
    base.update(rules_kwargs)
    return Watch(name="L2", keyword="switch white", mode="price", rules=Rules(**base))


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tmpdir()
        self.tmp = self._tmp.__enter__()
        self.store = Store(self.tmp / "state.db")
        # 去掉循环里的礼貌性 sleep，测试才跑得快
        self._sleep = mock.patch("mercari_watch.runner.time.sleep", lambda *a, **k: None)
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        self.store.close()
        self._tmp.__exit__(None, None, None)

    def build(self, watch: Watch, client: FakeClient) -> Runner:
        cfg = make_config(self.tmp, [watch])
        clients = {"mercari": client, "yahoo": client}
        return Runner(cfg, self.store, clients, EmailNotifier(cfg.email))

    def kinds(self) -> list[str]:
        return [row["kind"] for row in reversed(self.store.recent_events(200))]

    def clear_events(self) -> None:
        self.store.conn.execute("DELETE FROM events")
        self.store.conn.commit()


class L1Tests(RunnerTestCase):
    def test_first_run_builds_baseline_silently(self):
        client = FakeClient(search_items=[make_item("m1"), make_item("m2")])
        runner = self.build(make_l1(), client)
        report = runner.run_once()[0]

        self.assertEqual(report.fetched, 2)
        self.assertEqual(report.new_events, 0)
        self.assertEqual(self.kinds(), [])
        self.assertEqual(self.store.watch_state("L1")["baseline_done"], 1)

    def test_new_item_reported_on_second_run(self):
        client = FakeClient(search_items=[make_item("m1"), make_item("m2")])
        runner = self.build(make_l1(), client)
        runner.run_once()

        client.search_items.append(make_item("m3", price=25000))
        report = runner.run_once()[0]

        self.assertEqual(report.new_events, 1)
        self.assertEqual(self.kinds(), ["new_item"])
        self.assertEqual(self.store.get_sub("L1", "m3")["last_price"], 25000)

    def test_same_item_not_reported_twice(self):
        client = FakeClient(search_items=[make_item("m1")])
        runner = self.build(make_l1(), client)
        runner.run_once()
        client.search_items.append(make_item("m2"))
        runner.run_once()
        runner.run_once()
        self.assertEqual(self.kinds().count("new_item"), 1)

    def test_stable_state_fetches_only_first_page(self):
        watch = make_l1()
        watch.pages = 3
        client = FakeClient(search_items=[make_item("m1")])
        runner = self.build(watch, client)

        runner.run_once()  # 建基线：会翻 3 页
        self.assertEqual(client.search_calls, 1)  # 没有 next_token，1 页就停
        runner.run_once()
        self.assertEqual(client.search_calls, 2)

    def test_filtered_out_item_is_ignored(self):
        watch = make_l1()
        watch.filters = Filters(price_max=20000)
        client = FakeClient(search_items=[make_item("m1", price=30000)])
        runner = self.build(watch, client)
        runner.run_once()
        # 被过滤掉的商品不通知，但要记下来，否则每轮都会重新当成"新货"判定
        self.assertEqual(self.kinds(), [])
        self.assertEqual(self.store.count_subs("L1"), 1)


class L2Tests(RunnerTestCase):
    def test_first_run_locks_and_reports(self):
        client = FakeClient(search_items=[make_item("m1", price=30000)])
        runner = self.build(make_l2(), client)
        report = runner.run_once()[0]

        self.assertEqual(report.mode, "price")
        self.assertEqual(self.kinds(), ["locked"])
        locked = self.store.locked_subs("L2")
        self.assertEqual(len(locked), 1)
        self.assertEqual(locked[0]["item_id"], "m1")
        self.assertEqual(locked[0]["last_price"], 30000)

    def test_lock_picks_first_matching_candidate(self):
        watch = make_l2()
        watch.filters = Filters(price_max=29500)
        client = FakeClient(
            search_items=[make_item("m1", price=30000), make_item("m2", price=28000)]
        )
        self.build(watch, client).run_once()
        self.assertEqual(self.store.locked_subs("L2")[0]["item_id"], "m2")

    def test_price_drop_after_lock(self):
        client = FakeClient(search_items=[make_item("m1", price=30000)])
        runner = self.build(make_l2(), client)
        runner.run_once()
        self.clear_events()

        client.items["m1"] = make_item("m1", price=27000)
        runner.run_once()

        self.assertEqual(self.kinds(), ["price_drop"])
        self.assertEqual(self.store.locked_subs("L2")[0]["last_price"], 27000)

    def test_price_increase_reported(self):
        client = FakeClient(search_items=[make_item("m1", price=30000)])
        runner = self.build(make_l2(), client)
        runner.run_once()
        self.clear_events()

        client.items["m1"] = make_item("m1", price=33000)
        runner.run_once()
        self.assertEqual(self.kinds(), ["price_up"])

    def test_unchanged_price_is_silent(self):
        client = FakeClient(search_items=[make_item("m1", price=30000)])
        runner = self.build(make_l2(), client)
        runner.run_once()
        self.clear_events()

        client.items["m1"] = make_item("m1", price=30000)
        runner.run_once()
        self.assertEqual(self.kinds(), [])

    def test_sold_out_reported(self):
        client = FakeClient(search_items=[make_item("m1", price=30000)])
        runner = self.build(make_l2(), client)
        runner.run_once()
        self.clear_events()

        client.items["m1"] = make_item("m1", price=30000, status=STATUS_SOLD)
        runner.run_once()
        self.assertEqual(self.kinds(), ["sold_out"])

    def test_removed_item_reported_once(self):
        client = FakeClient(search_items=[make_item("m1", price=30000)])
        runner = self.build(make_l2(), client)
        runner.run_once()
        self.clear_events()

        client.items.clear()  # 详情页 404
        runner.run_once()
        self.assertEqual(self.kinds(), ["removed"])
        self.assertEqual(self.store.get_item("m1")["status"], "removed")

        # 已经确认下架，后续不再重复报、也不再请求
        self.clear_events()
        before = client.fetch_calls
        runner.run_once()
        self.assertEqual(self.kinds(), [])
        self.assertEqual(client.fetch_calls, before)

    def test_does_not_search_again_once_locked(self):
        client = FakeClient(search_items=[make_item("m1", price=30000)])
        runner = self.build(make_l2(), client)
        runner.run_once()
        self.assertEqual(client.search_calls, 1)
        client.items["m1"] = make_item("m1", price=29000)
        runner.run_once()
        self.assertEqual(client.search_calls, 1)  # 锁定后不再搜索

    def test_no_candidate_waits_for_next_round(self):
        client = FakeClient(search_items=[])
        runner = self.build(make_l2(), client)
        runner.run_once()
        self.assertEqual(self.kinds(), [])
        self.assertEqual(self.store.locked_subs("L2"), [])
        self.assertEqual(self.store.watch_state("L2")["baseline_done"], 0)


class DedupeTests(RunnerTestCase):
    """两个关键字命中同一件商品时，只发一条提醒。"""

    def test_same_item_from_two_watches_reported_once(self):
        w1, w2 = make_l1(), make_l1()
        w1.name, w2.name = "A", "B"
        cfg = make_config(self.tmp, [w1, w2])
        client = FakeClient(search_items=[make_item("m1")])
        sent = []

        class CapturingNotifier:
            enabled = True

            def send_events(self, events):
                sent.extend(events)

        runner = Runner(cfg, self.store, {"mercari": client, "yahoo": client}, CapturingNotifier())
        runner.run_once()  # 两个监控项各自建基线

        client.search_items.append(make_item("m2"))
        runner.run_once()

        self.assertEqual([e.item_id for e in sent], ["m2"])
        self.assertEqual([e.kind for e in sent], ["new_item"])
        # 两条事件都记了账，只是邮件里合并成一条
        self.assertEqual(self.store.pending_events(), [])


class RelockTests(RunnerTestCase):
    def test_unlock_then_lock_another(self):
        client = FakeClient(search_items=[make_item("m1", price=30000)])
        runner = self.build(make_l2(), client)
        runner.run_once()

        self.store.unlock("L2")
        client.search_items = [make_item("m9", price=26000)]
        client.items["m9"] = make_item("m9", price=26000)
        runner.run_once()

        locked = self.store.locked_subs("L2")
        self.assertEqual([s["item_id"] for s in locked], ["m9"])


if __name__ == "__main__":
    unittest.main()
