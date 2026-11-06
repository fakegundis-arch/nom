# nom: Bybit P2P bot with Nomba Business

A bot for running a USDT/NGN merchant desk on Bybit P2P, using a Nomba
Business account as the naira account.

## Modes

- **Monitor only** (`MONITOR_ONLY=true`, the default): the bot **never moves
  money**. The code that releases crypto, sends transfers, converts or
  withdraws refuses to run. It watches and tells you what to do.
- **Live** (`MONITOR_ONLY=false`): each automatic action has its own switch,
  all off by default: `AUTO_RELEASE`, `AUTO_PAY`, `AUTO_REFILL` (and
  `AUTO_ADS` for ad prices). Turn them on one at a time.

What it does in monitor-only mode:

| Situation | What the bot does |
|---|---|
| New SELL order | Alerts you: waiting for buyer to pay |
| Buyer marks paid and the money **has** arrived from the buyer's own name | ✅ "payment confirmed": you release in Bybit |
| Money arrived but from **a different name** | ⚠️ "possible third-party payment": check before releasing |
| Buyer marks paid but **no money** arrived | ⏳ "Do NOT release" |
| New BUY order | Looks up the seller's bank account name with Nomba and compares it with their Bybit KYC name, then tells you to pay |
| Order goes to appeal | 🚨 alert |
| Naira or USDT balance gets low | 📉 alert (and 📈 when it recovers) |
| Any naira credit into Nomba | 💰 alert |

## Automatic release and payment

**SELL orders (`AUTO_RELEASE`)**: when the buyer's payment has really arrived
in Nomba, for the exact amount, from a name that matches their Bybit KYC
name, the bot releases the USDT. Third-party payments are never released
automatically.

**BUY orders (`AUTO_PAY`)**: the bot looks up the seller's bank account with
Nomba. If the account holder's name matches the seller's KYC name and you
have the naira, it sends the transfer, waits for Nomba to confirm it, then
marks the order paid on Bybit.

Limits on every automatic action:

| Setting | Meaning |
|---|---|
| `AUTO_MAX_ORDER` | Bigger orders wait for you (`/release ID`, `/pay ID`) |
| `AUTO_NEW_COUNTERPARTY_MAX` | Lower cap for people you've never completed a trade with |
| `AUTO_DAILY_LIMIT` | Total naira the bot may handle alone per day |

**A seller is never paid twice.** Each BUY order gets one payout record and
one Nomba reference (`bybit-p2p-ORDER_ID`) *before* money moves. If a
transfer's result is unclear (timeout, dropped connection), the bot asks Nomba
about that reference instead of sending again.

If a BUY order is cancelled after the bot paid, you get a 🚨 alert at once.

## Naira refill through Nomba (`AUTO_REFILL`)

When free naira (Nomba balance minus naira owed on open BUY orders) falls
below `NAIRA_LOW_THRESHOLD`:

1. If your **Nomba USDT wallet** holds USDT, the bot gets a locked quote and,
   if the rate is at least `REFILL_MIN_RATE`, converts enough USDT to get back
   to `REFILL_TARGET_NAIRA` (max `REFILL_MAX_USDT_PER_DAY`).
2. If that wallet is empty and you've set the optional withdraw key, it sends
   USDT from Bybit to your Nomba USDT address (never below
   `USDT_LOW_THRESHOLD` on Bybit), then converts it when it lands.
3. Otherwise it tells you to move USDT yourself.

**It reacts right after each trade, not only on timers.** As soon as an order
completes or is cancelled, the bot pays a seller, releases USDT, or naira
arrives, it re-reads both balances and immediately re-runs the ad manager and
the refill. So if a trade leaves naira low, the BUY ad is shrunk or paused and
the refill starts *before* the next seller opens an order. (The regular checks
every 60 seconds / `REFILL_CHECK_SECONDS` still run as a backup.)

This uses Nomba's Global Payout endpoints (`/v1/global-payout/money/convert`
and `/v1/global-payout/exchange/authorize`), as in Nomba's official SDK.
**Confirm with Nomba that your account has a USDT wallet** and test it in the
sandbox first.

## Telegram commands

Only your own chat is answered.

| Command | Does |
|---|---|
| `/status` | Mode, balances, today's automatic total, naira owed |
| `/stop` / `/go` | Halt or resume ALL automatic releases, payments and refills |
| `/release ID` | Release a SELL order the bot held back |
| `/pay ID` | Pay a BUY order the bot held back |
| `/refill` | Try a naira refill now |
| `/prices`, `/pause`, `/resume` | Ad pricing plan; stop or restart ad changes |

## Ad pricing and inventory

Every minute the bot looks at everyone's USDT/NGN ads and works out prices
for your own BUY and SELL ads:

- **Competitive:** beats the merchant at your `TARGET_RANK` by `PRICE_STEP`
  (e.g. rank 2, ₦1). Tiny "bait" ads are ignored.
- **Never past your limits:** never sells below `SELL_MIN_PRICE`, never buys
  above `BUY_MAX_PRICE`, and always keeps at least `MIN_SPREAD` between your
  sell and buy price so each round trip makes money.
- **Only offers naira you have:** the BUY ad's maximum order is set to your
  Nomba balance minus naira you still owe on open BUY orders, minus
  `NAIRA_BUFFER`.
- **Refills naira automatically:** when naira gets low, the BUY ad goes
  offline and the SELL ad moves to the #1 price so buyers bring naira in.
  When naira recovers, the BUY ad comes back. Low USDT does the opposite.

With `AUTO_ADS=false` (the default) it only **suggests** changes on Telegram
(at most every 10 minutes). Set `AUTO_ADS=true` to let it edit your ads.
Editing ads never moves money. `/pause` stops all ad changes at once.

Your ads must use a **fixed price** for repricing; the bot leaves floating-price
ads alone and tells you.

## Safety rules built in

- **Release only on money that really arrived.** A Nomba webhook is checked
  for Nomba's signature, then the amount is fetched from Nomba's API, because
  the signature does not cover the amount.
- **Name match.** The sender's name must match the buyer's KYC name (word
  order, titles and bank prefixes like `SAMPLE/` are ignored). Anything else
  goes to you.
- **One credit pays one order.** A credit is claimed by the first order it
  matches and can't be counted twice. Resent webhooks are ignored.
- **Exact amount, inside the payment window** (default 30 minutes).
- **Replay protection.** Webhooks older than 5 minutes are rejected.
- **Main Bybit key without withdrawals**, locked to the VPS IP. Refill
  withdrawals use a separate key that can only send to whitelisted addresses.

## Layout

```
p2pbot/
  bot.py           main loop, webhook listener, alerts, Telegram commands
  autopilot.py     automatic release and payment, limits, approvals
  refill.py        USDT -> naira refills through Nomba
  ads.py           ad repricing and naira/USDT inventory
  bybit_client.py  Bybit P2P API (signed requests; paths from Bybit's pybit SDK)
  nomba_client.py  Nomba API (token, balance, name lookup, transactions, transfers)
  webhook.py       Nomba webhook signature check and parsing
  matcher.py       payment matching and name comparison
  db.py            SQLite: orders, credits, audit log
  telegram.py      alerts and commands
  config.py        settings from .env
tests/             78 tests, including Nomba's documented webhook signature vector
docs/DEPLOY.md     VPS setup step by step
```

## Run the tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## Deploy

See [docs/DEPLOY.md](docs/DEPLOY.md).

## Going live: do it in this order

1. Run in monitor-only mode against **Nomba sandbox** keys until alerts match
   what you see in Bybit and Nomba.
2. Switch to live Nomba keys, still `MONITOR_ONLY=true`, for a few days.
3. `MONITOR_ONLY=false` and `AUTO_RELEASE=true` with **small** limits
   (e.g. `AUTO_MAX_ORDER=50000`, `AUTO_DAILY_LIMIT=300000`).
4. Then `AUTO_PAY=true`, then `AUTO_REFILL=true`. Raise limits slowly.
5. Check these against Nomba's and Bybit's live docs (they were taken from
   official SDKs and docs, but not yet tried against a live account):
   - Bybit order fields (`buyerRealName`, `sellerRealName`, `paymentTermList`) and status codes.
   - Bybit ad fields (`priceType` 0 = fixed, `paymentTerms[].id`, `lastQuantity`,
     `userId` on online ads) and the ad list's `tokenId`/`currencyId`/`side`.
   - Nomba transaction status values (`SUCCESS`, `PAYMENT_SUCCESSFUL`).
