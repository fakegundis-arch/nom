"""Ad repricing and naira/USDT inventory.

Every REPRICE_SECONDS the manager:
  1. reads competitors' ads and your own,
  2. works out the price that puts you at TARGET_RANK, never past your limits,
  3. sizes the BUY ad to the naira you can actually pay,
  4. when naira runs low, pauses the BUY ad and pushes the SELL ad to the top
     (and the other way round when USDT runs low),
  5. applies the changes (AUTO_ADS on) or suggests them on Telegram (off).
"""

import logging
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

import requests

from .bybit_client import SIDE_BUY, SIDE_SELL, BybitError, BybitP2PClient
from .config import AdSettings

log = logging.getLogger(__name__)

CENT = Decimal("0.01")
# Bybit's fixed-price ads have priceType 0 (1 is floating).
FIXED_PRICE = "0"
SUGGESTION_GAP_SECONDS = 10 * 60


@dataclass
class Plan:
    sell_price: Decimal | None
    buy_price: Decimal | None
    buy_max_amount: Decimal | None   # naira; None = leave as is
    buy_online: bool
    sell_online: bool
    notes: list[str]


def competitor_prices(items: list[dict[str, Any]], my_user_id: str, min_usdt: Decimal,
                      side: int) -> list[Decimal]:
    """Other merchants' prices, best first. For SELL ads best = cheapest; for BUY ads best = highest."""
    prices = []
    for item in items:
        if str(item.get("userId", "")) == my_user_id:
            continue
        if Decimal(str(item.get("lastQuantity") or item.get("quantity") or "0")) < min_usdt:
            continue
        prices.append(Decimal(str(item["price"])))
    return sorted(prices, reverse=(side == SIDE_BUY))


def target_price(competitors: list[Decimal], side: int, rank: int, step: Decimal) -> Decimal | None:
    """Price that lands just ahead of the competitor currently at `rank`."""
    if not competitors:
        return None
    anchor = competitors[min(rank, len(competitors)) - 1]
    return anchor - step if side == SIDE_SELL else anchor + step


def plan(cfg: AdSettings, sell_competitors: list[Decimal], buy_competitors: list[Decimal],
         naira_balance: Decimal, naira_reserved: Decimal, usdt_balance: Decimal,
         naira_low: Decimal, usdt_low: Decimal, buy_min_amount: Decimal) -> Plan:
    notes: list[str] = []
    naira_short = naira_balance - naira_reserved < naira_low
    usdt_short = usdt_balance < usdt_low

    # Whichever side refills what's running low goes to the top of the page.
    sell_rank = 1 if naira_short else cfg.target_rank
    buy_rank = 1 if usdt_short else cfg.target_rank

    buy = target_price(buy_competitors, SIDE_BUY, buy_rank, cfg.price_step)
    sell = target_price(sell_competitors, SIDE_SELL, sell_rank, cfg.price_step)

    # Hard limits, then keep the spread so every round trip makes money.
    if buy is not None:
        buy = min(buy, cfg.buy_max_price)
    if sell is not None:
        sell = max(sell, cfg.sell_min_price)
    if buy is not None and sell is not None and sell - buy < cfg.min_spread:
        notes.append(f"Market spread is tighter than your ₦{cfg.min_spread} minimum; holding prices apart.")
        if naira_short:
            buy = sell - cfg.min_spread    # keep SELL competitive, give up on BUY
        else:
            sell = buy + cfg.min_spread
    if buy is not None:
        buy = buy.quantize(CENT, ROUND_DOWN)
    if sell is not None:
        sell = sell.quantize(CENT, ROUND_DOWN)

    # The BUY ad may only offer naira we really have.
    available = (naira_balance - naira_reserved - cfg.naira_buffer).quantize(Decimal("1"), ROUND_DOWN)
    buy_max = max(available, Decimal("0"))
    if cfg.buy_max_order > 0:
        buy_max = min(buy_max, cfg.buy_max_order)
    buy_online = buy_max >= buy_min_amount and not naira_short
    if naira_short:
        notes.append("Naira low: BUY ad paused, SELL ad pushed to #1 to bring naira in.")
    elif not buy_online:
        notes.append("Free naira is below the BUY ad minimum order: BUY ad paused.")
    if usdt_short:
        notes.append("USDT low: BUY ad pushed to #1 to bring USDT in.")

    return Plan(sell_price=sell, buy_price=buy, buy_max_amount=buy_max, buy_online=buy_online,
                sell_online=True, notes=notes)


def _num(value: Decimal) -> str:
    """1601.00 -> "1601", 1600.50 -> "1600.5" (never scientific notation)."""
    return f"{value.normalize():f}"


def update_body(detail: dict[str, Any], price: Decimal | None, max_amount: Decimal | None,
                action: str = "MODIFY") -> dict[str, Any]:
    """Re-send the ad as it is, changing only price and max amount."""
    payment_ids = [str(t.get("id")) for t in detail.get("paymentTerms") or [] if t.get("id") is not None]
    return {
        "id": str(detail["id"]),
        "priceType": str(detail.get("priceType", FIXED_PRICE)),
        "premium": str(detail.get("premium", "")),
        "price": _num(price) if price is not None else str(detail["price"]),
        "minAmount": str(detail["minAmount"]),
        "maxAmount": _num(max_amount) if max_amount is not None else str(detail["maxAmount"]),
        "remark": detail.get("remark", ""),
        "tradingPreferenceSet": detail.get("tradingPreferenceSet") or {},
        "paymentIds": payment_ids,
        "actionType": action,
        "quantity": str(detail.get("lastQuantity") or detail.get("quantity")),
        "paymentPeriod": str(detail.get("paymentPeriod", "15")),
    }


class AdManager:
    def __init__(self, cfg: AdSettings, bybit: BybitP2PClient, db, telegram,
                 naira_low: Decimal, usdt_low: Decimal):
        self.cfg = cfg
        self.bybit = bybit
        self.db = db
        self.tg = telegram
        self.naira_low = naira_low
        self.usdt_low = usdt_low
        self.paused = False
        self.last_plan: Plan | None = None
        self._last_run = 0.0
        self._user_id: str | None = None

    def enabled(self) -> tuple[bool, str]:
        if self.cfg.sell_min_price <= 0 or self.cfg.buy_max_price <= 0:
            return False, "Set SELL_MIN_PRICE and BUY_MAX_PRICE in .env to turn on ad pricing."
        if self.cfg.sell_min_price - self.cfg.buy_max_price < self.cfg.min_spread:
            return False, "SELL_MIN_PRICE must be at least MIN_SPREAD above BUY_MAX_PRICE."
        if self.paused:
            return False, "Ad management paused (/resume to restart)."
        return True, ""

    def _my_ads(self) -> dict[int, dict[str, Any]]:
        ads: dict[int, dict[str, Any]] = {}
        for ad in self.bybit.my_ads():
            if ad.get("tokenId") == self.cfg.token and ad.get("currencyId") == self.cfg.currency:
                ads.setdefault(int(ad["side"]), ad)
        return ads

    def tick(self, naira_balance: Decimal | None, usdt_balance: Decimal | None, now: bool = False) -> None:
        """`now` skips the timer: used right after a trade changes the balances."""
        if not now and time.time() - self._last_run < self.cfg.reprice_seconds:
            return
        self._last_run = time.time()
        ok, why = self.enabled()
        if not ok:
            if self.db.get_flag("ads:disabled") != why:
                self.db.set_flag("ads:disabled", why)
                self.tg.send(f"ℹ️ {why}")
            return
        self.db.set_flag("ads:disabled", "")
        if naira_balance is None or usdt_balance is None:
            return  # wait for the first balance check
        try:
            self._run(naira_balance, usdt_balance)
        except (BybitError, requests.RequestException, KeyError) as exc:
            log.warning("Ad update failed: %s", exc)
            self.db.log("ads_error", repr(exc))

    def _run(self, naira_balance: Decimal, usdt_balance: Decimal) -> None:
        if self._user_id is None:
            self._user_id = self.bybit.my_user_id()
        mine = self._my_ads()
        sell_items = self.bybit.online_ads(self.cfg.token, self.cfg.currency, SIDE_SELL)
        buy_items = self.bybit.online_ads(self.cfg.token, self.cfg.currency, SIDE_BUY)
        buy_detail = self.bybit.ad_detail(str(mine[SIDE_BUY]["id"])) if SIDE_BUY in mine else None
        sell_detail = self.bybit.ad_detail(str(mine[SIDE_SELL]["id"])) if SIDE_SELL in mine else None

        p = plan(
            self.cfg,
            competitor_prices(sell_items, self._user_id, self.cfg.min_competitor_usdt, SIDE_SELL),
            competitor_prices(buy_items, self._user_id, self.cfg.min_competitor_usdt, SIDE_BUY),
            naira_balance, self.db.naira_owed(), usdt_balance, self.naira_low, self.usdt_low,
            Decimal(str(buy_detail["minAmount"])) if buy_detail else Decimal("0"),
        )
        self.last_plan = p
        changes = []
        if sell_detail:
            changes += self._apply(SIDE_SELL, sell_detail, p.sell_price, None, p.sell_online)
        if buy_detail:
            changes += self._apply(SIDE_BUY, buy_detail, p.buy_price, p.buy_max_amount, p.buy_online)
        message = "\n".join(changes + p.notes)
        if not message or self.db.get_flag("ads:last_message") == message:
            return
        if not self.cfg.auto_ads:
            # Suggestions change with every market move; don't flood the chat.
            last = float(self.db.get_flag("ads:last_suggestion_at") or 0)
            if time.time() - last < SUGGESTION_GAP_SECONDS:
                return
            self.db.set_flag("ads:last_suggestion_at", str(time.time()))
        self.db.set_flag("ads:last_message", message)
        prefix = "🛠 Ads updated:" if self.cfg.auto_ads else "💡 Suggested ad changes (AUTO_ADS is off):"
        self.tg.send(f"{prefix}\n{message}")

    def _apply(self, side: int, detail: dict[str, Any], price: Decimal | None,
               max_amount: Decimal | None, online: bool) -> list[str]:
        name = "SELL" if side == SIDE_SELL else "BUY"
        ad_id = str(detail["id"])
        if str(detail.get("priceType", FIXED_PRICE)) != FIXED_PRICE:
            return [f"{name} ad uses a floating price; switch it to a fixed price for repricing."]
        offline_key = f"ads:offline:{ad_id}"
        is_offline = self.db.get_flag(offline_key) == "1"

        if not online:
            if is_offline:
                return []
            if self.cfg.auto_ads:
                self.bybit.take_ad_offline(ad_id)
                self.db.set_flag(offline_key, "1")
                self.db.log("ad_offline", f"{name} ad {ad_id} taken offline")
            return [f"{name} ad → offline"]

        current_price = Decimal(str(detail["price"]))
        current_max = Decimal(str(detail["maxAmount"]))
        new_price = price if price is not None and abs(price - current_price) >= self.cfg.price_step else None
        new_max = max_amount if max_amount is not None and max_amount != current_max else None
        if not is_offline and new_price is None and new_max is None:
            return []

        lines = []
        if is_offline:
            lines.append(f"{name} ad → back online")
        if new_price is not None:
            lines.append(f"{name} price ₦{current_price} → ₦{new_price}")
        if new_max is not None:
            lines.append(f"{name} max order ₦{current_max} → ₦{new_max}")
        if self.cfg.auto_ads:
            self.bybit.update_ad(update_body(detail, new_price, new_max, "ACTIVE" if is_offline else "MODIFY"))
            self.db.set_flag(offline_key, "0")
            self.db.log("ad_update", "; ".join(lines))
        return lines

    def describe(self) -> str:
        ok, why = self.enabled()
        if not ok:
            return why
        p = self.last_plan
        if p is None:
            return "No pricing plan yet; wait a minute."
        mode = "applying automatically" if self.cfg.auto_ads else "suggesting only (AUTO_ADS off)"
        return (f"Ads: {mode}\nSELL target ₦{p.sell_price}\nBUY target ₦{p.buy_price} "
                f"({'online' if p.buy_online else 'paused'}, max ₦{p.buy_max_amount})\n" + "\n".join(p.notes))
