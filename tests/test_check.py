from decimal import Decimal

from p2pbot.check import FAIL, PASS, SKIP, Options, main, run_checks
from p2pbot.nomba_client import NombaError


class FakeClient:
    def __init__(self, sandbox=True):
        self.sandbox = sandbox
        self.transfers, self.exchanges = [], []
        self.statuses = ["PROCESSING", "SUCCESS"]
        self.wallets = [{"currency": "NGN", "availableBalance": 5000}, {"currency": "USDT", "availableBalance": 10}]

    def _issue_token(self):
        pass

    def balance(self):
        return Decimal("1000000")

    def banks(self):
        return [{"name": "Nombank", "code": "090645"}]

    def lookup_account_name(self, account, code):
        return "JOHN DOE"

    def transfer_to_bank(self, amount, account, name, code, ref, narration):
        self.transfers.append((amount, account, name, code, ref))
        return {"status": "PROCESSING"}

    def get_transaction_by_reference(self, ref):
        return {"status": self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]}

    def currency_wallets(self):
        return self.wallets

    def quote_conversion(self, amount, currency, to_currency):
        return {"toAmount": "1590", "exchangeRateId": "r1", "feeAmount": 0}

    def exchange(self, amount, src, dst, rate_id, name, country, key):
        self.exchanges.append((amount, rate_id, name))
        return {"status": "PROCESSING"}


def statuses(results):
    return {r.name.split(" (")[0]: r.status for r in results}


FAST = dict(requery_wait=0)


def test_all_checks_pass_in_sandbox():
    client = FakeClient()
    results = run_checks(client, Options(convert=True, business_name="My Ventures Ltd", **FAST))
    assert all(r.status == PASS for r in results), [(r.name, r.status, r.detail) for r in results]
    assert client.transfers[0][2] == "JOHN DOE"   # pays the name the lookup returned
    assert client.exchanges == [(Decimal("1"), "r1", "My Ventures Ltd")]


def test_live_nomba_never_moves_money():
    client = FakeClient(sandbox=False)
    results = run_checks(client, Options(convert=True, business_name="X", **FAST))
    s = statuses(results)
    assert s["Send a test transfer"] == SKIP
    assert s["Convert 1 USDT -> NGN"] == SKIP
    assert client.transfers == [] and client.exchanges == []
    assert s["Read naira balance"] == PASS


def test_missing_usdt_wallet_is_reported():
    client = FakeClient()
    client.wallets = [{"currency": "NGN", "availableBalance": 5000}]
    s = statuses(run_checks(client, Options(**FAST)))
    assert s["Find a USDT wallet"] == FAIL


def test_a_failing_step_doesnt_stop_the_rest():
    client = FakeClient()

    def broken():
        raise NombaError("HTTP 401 invalid credentials")
    client.balance = broken
    results = run_checks(client, Options(transfer=False, **FAST))
    s = statuses(results)
    assert s["Read naira balance"] == FAIL
    assert s["List banks"] == PASS
    assert "401" in next(r.detail for r in results if r.status == FAIL)


def test_no_transfer_flag_is_read_only():
    client = FakeClient()
    run_checks(client, Options(transfer=False, **FAST))
    assert client.transfers == []


def test_main_reports_missing_settings(monkeypatch, tmp_path, capsys):
    for name in ("NOMBA_CLIENT_ID", "NOMBA_CLIENT_SECRET", "NOMBA_ACCOUNT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    assert main([]) == 2
    assert "Missing in .env" in capsys.readouterr().out
