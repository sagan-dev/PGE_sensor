"""Customer, account and PPE numbers are strings; leading zeros are significant."""
from conftest import pge_api


def test_leading_zero_is_kept():
    assert pge_api.normalize_number("01234567") == "01234567"
    assert pge_api.normalize_number("001234567") == "001234567"
    assert pge_api.normalize_number(" 0123-45 67 ") == "01234567"


def test_leading_zero_variants_are_different_contracts():
    # numbers differing only by a leading zero must never be merged
    assert not pge_api.same_number("01234567", "001234567")
    assert pge_api.same_number("01234567", " 01234567 ")
    assert pge_api.same_number(1234567, "1234567")


def test_find_account_exact_match_wins():
    listed = ["01234567", "001234567"]
    assert pge_api.find_account_id(listed, "01234567") == "01234567"
    assert pge_api.find_account_id(listed, "001234567") == "001234567"


def test_find_account_legacy_int_matches_only_when_unambiguous():
    # entry stored as int by an older release, PGE now lists it with a zero
    assert pge_api.find_account_id(["01234567", "7654321"], 1234567) == "01234567"
    # both variants listed: a stored 1234567 cannot be resolved safely
    assert pge_api.find_account_id(["01234567", "001234567"], 1234567) is None
    assert pge_api.find_account_id([], "01234567") is None


def test_bool_and_empty_are_not_numbers():
    assert pge_api.normalize_number(True) is None
    assert pge_api.normalize_number("  ") is None


def test_numeric_never_invents_zero():
    assert pge_api.numeric(None) is None
    assert pge_api.numeric("") is None
    assert pge_api.numeric("abc") is None
    assert pge_api.numeric("12,50") == 12.5
    assert pge_api.numeric(0) == 0.0
