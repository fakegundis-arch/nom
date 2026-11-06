from decimal import Decimal

from p2pbot.matcher import Credit, Decision, NameMatch, compare_names, match_sell_order

ORDER_AT = 1_700_000_000.0
WINDOW = 30 * 60


def credit(txn: str, amount: str, name: str, offset: float = 60, used: str | None = None) -> Credit:
    return Credit(txn, Decimal(amount), name, ORDER_AT + offset, used)


def test_names_in_any_order_and_case_match_exactly():
    assert compare_names("DOE JOHN", "John Doe") == NameMatch.EXACT


def test_bank_prefixes_titles_and_punctuation_are_ignored():
    assert compare_names("SAMPLE/JOHN DOE", "John Doe") == NameMatch.STRONG
    assert compare_names("MR. DOE, JOHN", "John Doe") == NameMatch.EXACT


def test_missing_middle_name_is_strong():
    assert compare_names("John Ade Doe", "John Doe") == NameMatch.STRONG


def test_one_shared_word_is_weak_and_none_is_none():
    assert compare_names("John Smith", "John Doe") == NameMatch.WEAK
    assert compare_names("Mary Jane", "John Doe") == NameMatch.NONE
    assert compare_names("", "John Doe") == NameMatch.NONE


def test_matching_credit_is_paid():
    result = match_sell_order(Decimal("640000"), "John Doe", ORDER_AT, WINDOW,
                              [credit("t1", "640000", "DOE JOHN")])
    assert result.decision == Decision.PAID
    assert result.credit.txn_id == "t1"


def test_third_party_payment_goes_to_review():
    result = match_sell_order(Decimal("640000"), "John Doe", ORDER_AT, WINDOW,
                              [credit("t1", "640000", "Mary Jane Stores")])
    assert result.decision == Decision.REVIEW
    assert "third-party" in result.reason


def test_wrong_amount_late_or_used_credits_are_ignored():
    credits = [
        credit("short", "639999", "John Doe"),
        credit("late", "640000", "John Doe", offset=WINDOW + 1),
        credit("early", "640000", "John Doe", offset=-3600),
        credit("used", "640000", "John Doe", used="other-order"),
    ]
    result = match_sell_order(Decimal("640000"), "John Doe", ORDER_AT, WINDOW, credits)
    assert result.decision == Decision.NOT_PAID


def test_name_match_is_preferred_over_earlier_stranger():
    credits = [credit("stranger", "640000", "Mary Jane", offset=30),
               credit("buyer", "640000", "John Doe", offset=90)]
    result = match_sell_order(Decimal("640000"), "John Doe", ORDER_AT, WINDOW, credits)
    assert result.decision == Decision.PAID
    assert result.credit.txn_id == "buyer"
