"""Check your Nomba setup without touching Bybit.

    python -m p2pbot.check                      # all checks
    python -m p2pbot.check --account 0123456789 --bank-code 058
    python -m p2pbot.check --no-transfer        # read-only
    python -m p2pbot.check --convert            # also try a 1 USDT -> NGN conversion

Only the NOMBA_* settings are needed (Bybit and Telegram keys aren't read).
Sending a test transfer or converting only happens when NOMBA_BASE_URL is
Nomba's sandbox; against live Nomba every money-moving step is skipped.
Secrets are never printed.
"""

import argparse
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

import requests
from dotenv import load_dotenv

from .nomba_client import NombaClient, NombaError

# From Nomba's own webhook example; may not exist in every sandbox.
DEFAULT_TEST_ACCOUNT = "0000000000"
DEFAULT_TEST_BANK = "090645"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
DONE = {"SUCCESS", "PAYMENT_SUCCESSFUL"}


@dataclass
class Result:
    name: str
    status: str
    detail: str


@dataclass
class Options:
    account: str = DEFAULT_TEST_ACCOUNT
    bank_code: str = DEFAULT_TEST_BANK
    amount: Decimal = Decimal("100")
    transfer: bool = True
    convert: bool = False
    business_name: str = ""
    requery_tries: int = 5
    requery_wait: float = 3.0


def run_checks(client: NombaClient, opts: Options, out: Callable[[Result], None] = lambda r: None) -> list[Result]:
    results: list[Result] = []
    found: dict[str, Any] = {}

    def step(name: str, fn: Callable[[], tuple[str, str]]) -> None:
        try:
            status, detail = fn()
        except (NombaError, requests.RequestException, KeyError, ValueError, TypeError) as exc:
            status, detail = FAIL, str(exc)
        result = Result(name, status, detail)
        results.append(result)
        out(result)

    sandbox = client.sandbox
    money_note = "" if sandbox else "skipped: NOMBA_BASE_URL is live Nomba, not the sandbox"

    def login():
        client._issue_token()
        return PASS, "got an access token"

    def balance():
        found["balance"] = client.balance()
        return PASS, f"₦{found['balance']}"

    def banks():
        rows = client.banks()
        sample = ", ".join(f"{b.get('name')} ({b.get('code')})" for b in rows[:3])
        return (PASS, f"{len(rows)} banks, e.g. {sample}") if rows else (FAIL, "empty bank list")

    def lookup():
        name = client.lookup_account_name(opts.account, opts.bank_code)
        found["account_name"] = name
        return (PASS, f"{opts.account} at {opts.bank_code} belongs to {name!r}") if name else \
            (FAIL, "lookup returned no account name")

    def transfer():
        if not opts.transfer:
            return SKIP, "skipped (--no-transfer)"
        if not sandbox:
            return SKIP, money_note
        if not found.get("account_name"):
            return SKIP, "skipped: the account lookup didn't give a name"
        ref = f"check-{int(time.time())}"
        found["reference"] = ref
        result = client.transfer_to_bank(opts.amount, opts.account, found["account_name"], opts.bank_code,
                                         ref, "p2pbot sandbox check")
        return PASS, f"sent ₦{opts.amount}, reference {ref}, status {result.get('status')}"

    def requery():
        ref = found.get("reference")
        if not ref:
            return SKIP, "no test transfer to look up"
        status = ""
        for attempt in range(opts.requery_tries):
            txn = client.get_transaction_by_reference(ref)
            status = str(txn.get("status", "")).upper()
            if status in DONE or status in ("REFUND", "CANCELLED"):
                break
            if attempt < opts.requery_tries - 1:
                time.sleep(opts.requery_wait)
        if status in DONE:
            return PASS, f"Nomba reports {status} for {ref}"
        return FAIL, f"Nomba reports {status or 'no status'} for {ref}"

    def wallets():
        rows = client.currency_wallets()
        found["wallets"] = rows
        names = ", ".join(f"{w.get('currency')}: {w.get('availableBalance', w.get('balance'))}" for w in rows)
        has_usdt = any(str(w.get("currency", "")).upper() == "USDT" for w in rows)
        if has_usdt:
            return PASS, f"USDT wallet found ({names})"
        return FAIL, f"no USDT wallet ({names or 'no wallets'}): the refill can't work until Nomba enables one"

    def quote():
        q = client.quote_conversion(Decimal("1"), "USDT", "NGN")
        found["rate_id"] = q.get("exchangeRateId")
        return PASS, f"1 USDT ≈ ₦{q.get('toAmount')} (fee {q.get('feeAmount', '?')}, rate id {found['rate_id']})"

    def convert():
        if not opts.convert:
            return SKIP, "skipped (add --convert to try 1 USDT -> NGN)"
        if not sandbox:
            return SKIP, money_note
        if not opts.business_name:
            return SKIP, "skipped: set BUSINESS_NAME in .env"
        if not found.get("rate_id"):
            return SKIP, "skipped: no quote"
        result = client.exchange(Decimal("1"), "USDT", "NGN", str(found["rate_id"]), opts.business_name,
                                 "NG", f"check-{int(time.time())}")
        return PASS, f"conversion accepted, status {result.get('status')}"

    step("Log in (client id + secret + account id)", login)
    step("Read naira balance", balance)
    step("List banks", banks)
    step("Look up an account name (seller check)", lookup)
    step("Send a test transfer", transfer)
    step("Check the transfer by our reference", requery)
    step("Find a USDT wallet (refill)", wallets)
    step("Quote 1 USDT -> NGN (refill)", quote)
    step("Convert 1 USDT -> NGN (refill)", convert)
    return results


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m p2pbot.check", description="Check the Nomba side of the bot.")
    p.add_argument("--account", default=DEFAULT_TEST_ACCOUNT, help="account number to look up / pay")
    p.add_argument("--bank-code", default=DEFAULT_TEST_BANK, help="that account's bank code")
    p.add_argument("--amount", default="100", help="test transfer amount in naira (sandbox only)")
    p.add_argument("--no-transfer", action="store_true", help="don't send the test transfer")
    p.add_argument("--convert", action="store_true", help="also convert 1 USDT to NGN (sandbox only)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    load_dotenv()
    missing = [n for n in ("NOMBA_CLIENT_ID", "NOMBA_CLIENT_SECRET", "NOMBA_ACCOUNT_ID") if not os.environ.get(n)]
    if missing:
        print(f"Missing in .env: {', '.join(missing)}")
        return 2
    base_url = os.environ.get("NOMBA_BASE_URL") or "https://sandbox.nomba.com"
    sandbox = "sandbox" in base_url
    client = NombaClient(os.environ["NOMBA_CLIENT_ID"], os.environ["NOMBA_CLIENT_SECRET"],
                         os.environ["NOMBA_ACCOUNT_ID"], base_url,
                         # Money-moving calls are only unlocked against the sandbox.
                         monitor_only=not sandbox)
    print(f"Nomba check against {base_url} ({'SANDBOX' if sandbox else 'LIVE: read-only'})\n")
    icons = {PASS: "✅", FAIL: "❌", SKIP: "⏭ "}
    opts = Options(account=args.account, bank_code=args.bank_code, amount=Decimal(args.amount),
                   transfer=not args.no_transfer, convert=args.convert,
                   business_name=os.environ.get("BUSINESS_NAME", ""))
    results = run_checks(client, opts, lambda r: print(f"{icons[r.status]} {r.name}: {r.detail}"))
    failed = sum(r.status == FAIL for r in results)
    passed = sum(r.status == PASS for r in results)
    print(f"\n{passed} passed, {failed} failed, {len(results) - passed - failed} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
