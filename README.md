# nom: Bybit P2P bot with Nomba Business

A bot for running a USDT/NGN merchant desk on Bybit P2P, using a Nomba
Business account as the naira account.

## Current stage: Phase 1, monitor only

Phase 1 **never moves money**. While `MONITOR_ONLY=true` (the default), the
code that releases crypto, marks orders paid or sends transfers refuses to
run. The bot watches and tells you what to do on Telegram:

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

Telegram commands (only from your own chat): `/status`, `/balance`, `/help`.

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
- **Bybit key without withdrawals**, locked to the VPS IP.

## Layout

```
p2pbot/
  bot.py           main loop, webhook listener, alerts
  bybit_client.py  Bybit P2P API (signed requests)
  nomba_client.py  Nomba API (token, balance, name lookup, transactions, transfers)
  webhook.py       Nomba webhook signature check and parsing
  matcher.py       payment matching and name comparison
  db.py            SQLite: orders, credits, audit log
  telegram.py      alerts and commands
  config.py        settings from .env
tests/             31 tests, including Nomba's documented webhook signature vector
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

## Before turning on real money (later phases)

1. Nomba's **written confirmation** that crypto P2P is allowed on your account.
2. Phase 1 running for a while with alerts that match what you see in Bybit and Nomba.
3. Check these against Nomba's and Bybit's live docs (they were taken from
   official SDKs and docs, but not yet tried against a live account):
   - Bybit order fields (`buyerRealName`, `sellerRealName`, `paymentTermList`) and status codes.
   - Bybit order detail path `/v5/p2p/order/info`.
   - Nomba transaction status values (`SUCCESS`, `PAYMENT_SUCCESSFUL`).
