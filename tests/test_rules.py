import unittest

from mercari_watch.config import Filters, Rules, Watch
from mercari_watch.models import STATUS_ON_SALE, STATUS_SOLD, Item
from mercari_watch.rules import evaluate_lock, evaluate_new_listing, evaluate_price_change, evaluate_status


def make_watch(mode: str = "new", **rules_kwargs) -> Watch:
    base = dict(
        new_item=True,
        price_drop=True,
        price_increase=False,
        price_drop_percent=5,
        price_drop_absolute=0,
        below_price=28000,
        status_change=True,
        alert_on_first_run=False,
    )
    base.update(rules_kwargs)
    return Watch(name="t", keyword="switch", mode=mode, rules=Rules(**base))


def item(price=30000, name="Nintendo Switch 有機EL 本体", item_id="m12345678901") -> Item:
    return Item(id=item_id, name=name, price=price, status=STATUS_ON_SALE)


def sub(last_price=30000, below_alerted=0) -> dict:
    return {"last_price": last_price, "below_alerted": below_alerted, "title": "x", "url": "u"}


class NewListingTests(unittest.TestCase):
    def test_new_item_emits_event(self):
        events = evaluate_new_listing(make_watch(), item(), None, first_run=False)
        self.assertEqual([e.kind for e in events], ["new_item"])

    def test_already_subscribed_is_silent(self):
        events = evaluate_new_listing(make_watch(), item(), sub(), first_run=False)
        self.assertEqual(events, [])

    def test_first_run_is_silent_by_default(self):
        events = evaluate_new_listing(make_watch(), item(), None, first_run=True)
        self.assertEqual(events, [])

    def test_first_run_can_alert(self):
        watch = make_watch(alert_on_first_run=True)
        events = evaluate_new_listing(watch, item(), None, first_run=True)
        self.assertEqual([e.kind for e in events], ["new_item"])

    def test_excluded_keyword_blocks_event(self):
        watch = make_watch()
        watch.filters = Filters(exclude_keywords=["ジャンク"])
        self.assertEqual(
            evaluate_new_listing(watch, item(name="ジャンク Switch"), None, first_run=False), []
        )

    def test_price_range_blocks_event(self):
        watch = make_watch()
        watch.filters = Filters(price_max=20000)
        self.assertEqual(evaluate_new_listing(watch, item(price=30000), None, first_run=False), [])

    def test_new_item_below_target_alerts_too(self):
        events = evaluate_new_listing(make_watch(), item(price=25000), None, first_run=False)
        self.assertEqual([e.kind for e in events], ["new_item", "below_price"])


class PriceChangeTests(unittest.TestCase):
    def test_drop_over_threshold(self):
        events = evaluate_price_change(make_watch(mode="price"), item(price=30000), sub(40000))
        self.assertEqual([e.kind for e in events], ["price_drop"])
        self.assertEqual(events[0].old_price, 40000)

    def test_small_drop_under_threshold_ignored(self):
        events = evaluate_price_change(make_watch(mode="price"), item(price=29500), sub(30000, 1))
        self.assertEqual(events, [])

    def test_absolute_threshold(self):
        watch = make_watch(mode="price", price_drop_percent=0, price_drop_absolute=2000)
        self.assertEqual(
            [e.kind for e in evaluate_price_change(watch, item(price=27000), sub(30000, 1))],
            ["price_drop"],
        )
        self.assertEqual(evaluate_price_change(watch, item(price=29000), sub(30000, 1)), [])

    def test_any_drop_when_thresholds_zero(self):
        watch = make_watch(mode="price", price_drop_percent=0, price_drop_absolute=0, below_price=0)
        self.assertEqual(
            [e.kind for e in evaluate_price_change(watch, item(price=29999), sub(30000, 1))],
            ["price_drop"],
        )

    def test_increase_ignored_by_default(self):
        self.assertEqual(evaluate_price_change(make_watch(mode="price"), item(price=35000), sub()), [])

    def test_increase_reported_when_enabled(self):
        watch = make_watch(mode="price", price_increase=True, below_price=0)
        events = evaluate_price_change(watch, item(price=35000), sub(30000, 1))
        self.assertEqual([e.kind for e in events], ["price_up"])
        self.assertIn("+5,000", events[0].detail)

    def test_unchanged_price_is_silent(self):
        self.assertEqual(evaluate_price_change(make_watch(mode="price"), item(price=30000), sub()), [])

    def test_crossing_target_alerts_once(self):
        watch = make_watch(mode="price", price_drop=False, price_increase=False)
        events = evaluate_price_change(watch, item(price=27000), sub(31000, 0))
        self.assertEqual([e.kind for e in events], ["below_price"])

    def test_already_alerted_is_quiet(self):
        watch = make_watch(mode="price", price_drop=False, price_increase=False)
        self.assertEqual(evaluate_price_change(watch, item(price=26000), sub(27000, 1)), [])

    def test_drop_and_below_can_both_fire(self):
        watch = make_watch(mode="price", price_increase=True)
        events = evaluate_price_change(watch, item(price=27000), sub(31000, 0))
        self.assertEqual([e.kind for e in events], ["price_drop", "below_price"])

    def test_filters_apply_to_price_watch(self):
        watch = make_watch(mode="price")
        watch.filters = Filters(exclude_keywords=["ジャンク"])
        self.assertEqual(
            evaluate_price_change(watch, item(price=20000, name="ジャンク品"), sub(30000, 1)), []
        )


class StatusTests(unittest.TestCase):
    def test_sold_out(self):
        events = evaluate_status(make_watch(), "m1", "t", "u", STATUS_ON_SALE, STATUS_SOLD)
        self.assertEqual([e.kind for e in events], ["sold_out"])

    def test_removed(self):
        events = evaluate_status(make_watch(), "m1", "t", "u", STATUS_ON_SALE, "removed")
        self.assertEqual([e.kind for e in events], ["removed"])

    def test_back_on_sale(self):
        events = evaluate_status(make_watch(), "m1", "t", "u", STATUS_SOLD, STATUS_ON_SALE)
        self.assertEqual([e.kind for e in events], ["back_on_sale"])

    def test_unknown_status_ignored(self):
        self.assertEqual(
            evaluate_status(make_watch(), "m1", "t", "u", STATUS_ON_SALE, "unknown"), []
        )

    def test_disabled_rule(self):
        events = evaluate_status(
            make_watch(status_change=False), "m1", "t", "u", STATUS_ON_SALE, STATUS_SOLD
        )
        self.assertEqual(events, [])


class LockTests(unittest.TestCase):
    def test_lock_event(self):
        events = evaluate_lock(make_watch(mode="price"), item(price=30000))
        self.assertEqual([e.kind for e in events], ["locked"])
        self.assertIn("30,000", events[0].detail)


if __name__ == "__main__":
    unittest.main()
