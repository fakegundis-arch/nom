from decimal import Decimal

from p2pbot.ads import AdManager, competitor_prices, plan, target_price, update_body
from p2pbot.bybit_client import SIDE_BUY, SIDE_SELL
from p2pbot.config import AdSettings
from p2pbot.db import Database

D = Decimal
# Limits wide enough that the market prices below fall inside them.
WIDE = AdSettings(sell_min_price=D("1598"), buy_max_price=D("1585"), min_spread=D("13"),
                  price_step=D("1"), target_rank=2, naira_buffer=D("100000"))


def run_plan(cfg=WIDE, sells=("1600", "1602", "1605"), buys=("1580", "1578", "1575"),
             naira="2000000", owed="0", usdt="1000"):
    return plan(cfg, [D(p) for p in sells], [D(p) for p in buys], D(naira), D(owed), D(usdt),
                naira_low=D("500000"), usdt_low=D("300"), buy_min_amount=D("50000"))


def test_competitors_skip_my_ads_and_bait_ads_and_sort_best_first():
    items = [
        {"userId": "me", "price": "1590", "lastQuantity": "1000"},
        {"userId": "a", "price": "1605", "lastQuantity": "500"},
        {"userId": "b", "price": "1600", "lastQuantity": "500"},
        {"userId": "c", "price": "1550", "lastQuantity": "5"},  # bait: too small
    ]
    assert competitor_prices(items, "me", D("50"), SIDE_SELL) == [D("1600"), D("1605")]
    assert competitor_prices(items, "me", D("50"), SIDE_BUY) == [D("1605"), D("1600")]


def test_target_price_beats_the_competitor_at_rank():
    sells = [D("1600"), D("1602"), D("1605")]
    assert target_price(sells, SIDE_SELL, 1, D("1")) == D("1599")
    assert target_price(sells, SIDE_SELL, 2, D("1")) == D("1601")
    assert target_price(sells, SIDE_SELL, 9, D("1")) == D("1604")  # fewer ads than the rank
    assert target_price([D("1580")], SIDE_BUY, 1, D("1")) == D("1581")
    assert target_price([], SIDE_SELL, 1, D("1")) is None


def test_normal_market_targets_rank_two_on_both_sides():
    p = run_plan()
    assert p.sell_price == D("1601")
    assert p.buy_price == D("1579")
    assert p.buy_online and p.notes == []


def test_never_sells_below_floor_or_buys_above_ceiling():
    cfg = AdSettings(sell_min_price=D("1610"), buy_max_price=D("1570"), min_spread=D("15"), target_rank=1)
    p = run_plan(cfg)
    assert p.sell_price == D("1610")
    assert p.buy_price == D("1570")


def test_spread_is_kept_when_market_is_tight():
    cfg = AdSettings(**{**WIDE.__dict__, "min_spread": D("20")})
    p = run_plan(cfg, sells=("1590",), buys=("1585",))
    assert p.sell_price - p.buy_price == D("20")
    assert any("tighter" in n for n in p.notes)


def test_buy_ad_only_offers_naira_we_have():
    p = run_plan(naira="1000000", owed="300000")
    assert p.buy_max_amount == D("600000")  # 1,000,000 - 300,000 owed - 100,000 buffer


def test_buy_ad_respects_max_order_cap():
    cfg = AdSettings(**{**WIDE.__dict__, "buy_max_order": D("250000")})
    assert run_plan(cfg).buy_max_amount == D("250000")


def test_low_naira_pauses_buy_and_pushes_sell_to_top():
    p = run_plan(naira="550000", owed="100000")  # 450k free < 500k alert level
    assert not p.buy_online
    assert p.sell_price == D("1599")  # rank 1
    assert any("Naira low" in n for n in p.notes)


def test_low_usdt_pushes_buy_to_top():
    p = run_plan(usdt="100")
    assert p.buy_price == D("1581")
    assert any("USDT low" in n for n in p.notes)


def test_update_body_keeps_ad_settings_and_changes_price():
    detail = {"id": 7, "priceType": 0, "premium": "", "price": "1600", "minAmount": "50000",
              "maxAmount": "2000000", "remark": "Fast release", "tradingPreferenceSet": {"isKyc": 1},
              "paymentTerms": [{"id": "6558"}, {"id": 99}], "lastQuantity": "800", "paymentPeriod": 15}
    body = update_body(detail, D("1601"), None)
    assert body["price"] == "1601"
    assert body["maxAmount"] == "2000000"
    assert body["paymentIds"] == ["6558", "99"]
    assert body["actionType"] == "MODIFY"
    assert body["remark"] == "Fast release"


# --- AdManager against a fake Bybit ---

class FakeBybit:
    def __init__(self):
        self.updates, self.offline = [], []
        self.ads = {
            "s1": {"id": "s1", "side": 1, "tokenId": "USDT", "currencyId": "NGN", "priceType": 0, "price": "1610",
                   "minAmount": "10000", "maxAmount": "1000000", "paymentTerms": [{"id": "1"}],
                   "lastQuantity": "500", "paymentPeriod": 15},
            "b1": {"id": "b1", "side": 0, "tokenId": "USDT", "currencyId": "NGN", "priceType": 0, "price": "1570",
                   "minAmount": "50000", "maxAmount": "1000000", "paymentTerms": [{"id": "1"}],
                   "lastQuantity": "500", "paymentPeriod": 15},
        }

    def my_user_id(self):
        return "me"

    def my_ads(self):
        return list(self.ads.values())

    def ad_detail(self, item_id):
        return self.ads[item_id]

    def online_ads(self, token, currency, side):
        prices = ["1600", "1602", "1605"] if side == SIDE_SELL else ["1580", "1578", "1575"]
        return [{"userId": f"u{i}", "price": p, "lastQuantity": "500"} for i, p in enumerate(prices)]

    def update_ad(self, body):
        self.updates.append(body)

    def take_ad_offline(self, item_id):
        self.offline.append(item_id)


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def manager(auto=True):
    cfg = AdSettings(**{**WIDE.__dict__, "auto_ads": auto, "reprice_seconds": 0})
    return AdManager(cfg, FakeBybit(), Database(":memory:"), FakeTelegram(), D("500000"), D("300"))


def test_auto_mode_reprices_both_ads():
    m = manager()
    m.tick(D("2000000"), D("1000"))
    by_id = {u["id"]: u for u in m.bybit.updates}
    assert by_id["s1"]["price"] == "1601"
    assert by_id["b1"]["price"] == "1579"
    assert by_id["b1"]["maxAmount"] == "1900000"
    assert "Ads updated" in m.tg.sent[-1]


def test_suggest_mode_changes_nothing():
    m = manager(auto=False)
    m.tick(D("2000000"), D("1000"))
    assert m.bybit.updates == [] and m.bybit.offline == []
    assert "Suggested" in m.tg.sent[-1]


def test_low_naira_takes_buy_ad_offline_then_relists():
    m = manager()
    m.tick(D("400000"), D("1000"))
    assert m.bybit.offline == ["b1"]
    m.tick(D("400000"), D("1000"))
    assert m.bybit.offline == ["b1"]  # not taken offline twice
    m.tick(D("3000000"), D("1000"))
    relist = [u for u in m.bybit.updates if u["id"] == "b1"][-1]
    assert relist["actionType"] == "ACTIVE"


def test_pricing_stays_off_until_limits_are_set():
    m = AdManager(AdSettings(reprice_seconds=0), FakeBybit(), Database(":memory:"), FakeTelegram(),
                  D("500000"), D("300"))
    m.tick(D("2000000"), D("1000"))
    assert m.bybit.updates == []
    assert "SELL_MIN_PRICE" in m.tg.sent[-1]


def test_pause_stops_changes():
    m = manager()
    m.paused = True
    m.tick(D("2000000"), D("1000"))
    assert m.bybit.updates == []


def test_floating_price_ad_is_left_alone():
    m = manager()
    m.bybit.ads["s1"]["priceType"] = 1
    m.tick(D("2000000"), D("1000"))
    assert all(u["id"] != "s1" for u in m.bybit.updates)
    assert any("floating" in t for t in m.tg.sent)


def test_numbers_are_sent_without_trailing_zeros_or_exponents():
    from p2pbot.ads import _num
    assert _num(D("1601.00")) == "1601"
    assert _num(D("1600")) == "1600"
    assert _num(D("1600.50")) == "1600.5"
