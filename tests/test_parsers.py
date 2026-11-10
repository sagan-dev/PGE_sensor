"""Parsers on synthetic fixtures (no personal data)."""
from datetime import date

import pytest

from conftest import fixture_json, pge_api


def test_billing_accounts_keep_string_ids_and_status():
    accounts = pge_api.parse_billing_accounts(fixture_json("billing_accounts_two_contracts.json"))
    assert [a.id for a in accounts] == ["01234567", "001234567"]
    assert [a.contract_status for a in accounts] == ["ended", "active"]
    assert accounts[0].ppe_numbers == accounts[1].ppe_numbers == ["PL0000000000000000000001"]
    assert accounts[0].label.endswith("(umowa zakończona)")


def test_billing_accounts_without_flags_are_unknown():
    accounts = pge_api.parse_billing_accounts(fixture_json("billing_accounts_no_flags.json"))
    assert {a.contract_status for a in accounts} == {"unknown"}
    assert accounts[0].id == "1234567"


def test_unexpected_payloads_raise():
    with pytest.raises(pge_api.PgeApiError):
        pge_api.parse_billing_accounts([])
    with pytest.raises(pge_api.PgeApiError):
        pge_api.parse_documents_response({"documents": "x"})
    with pytest.raises(pge_api.PgeApiError):
        pge_api.parse_login_response({"token": "x"})


def test_balance_info_uses_real_invoice_not_forecast():
    docs = pge_api.parse_documents_response(fixture_json("documents_new_contract.json"))
    info = pge_api.build_balance_info(fixture_json("balance.json"), docs, account_key="001234567")
    assert info.amount == 123.45
    # newest document (a forecast) drives payment fields
    assert info.invoice_number == "TEST/FV/0002"
    assert info.current_payment == 111.11
    assert info.due_date == date(2026, 10, 20)
    # but energy comes from the newest real INVOICE
    assert info.consumed_energy == 321.0
    assert info.feed_in_energy == 45.5
    assert info.billing_period == "20.05.2026 - 31.08.2026"
    assert info.billing_period_start == date(2026, 5, 20)
    assert info.ppe == "PL0000000000000000000001"
    assert [i.number for i in info.invoices] == ["TEST/FV/0002", "TEST/FV/0001"]
    assert info.invoices[1].as_attribute()["contract"] == "001234567"
    assert info.invoices[1].period_from == date(2026, 5, 20)


def test_missing_values_are_none_not_zero():
    info = pge_api.build_balance_info(fixture_json("balance_empty.json"), [])
    assert info.amount is None
    assert info.invoice_amount is None
    assert info.consumed_energy is None
    assert info.due_date is None
    assert info.invoices == []


def test_several_ppe_without_hint_publish_no_energy():
    doc = fixture_json("documents_new_contract.json")["documents"][1]
    doc["ppmConsumptions"].append(dict(doc["ppmConsumptions"][0], ppmNumber="PL0000000000000000000002"))
    info = pge_api.build_balance_info({"totalAmount": 1}, [doc])
    assert info.consumed_energy is None
    info = pge_api.build_balance_info({"totalAmount": 1}, [doc], ppe="PL0000000000000000000002")
    assert info.ppe == "PL0000000000000000000002"


def test_date_parsing_variants():
    assert pge_api.parse_date("2026-05-20T00:00:00.000+0000") == date(2026, 5, 20)
    assert pge_api.parse_date("2026-05-20 10:11:12") == date(2026, 5, 20)
    assert pge_api.parse_date("20.05.2026") == date(2026, 5, 20)
    assert pge_api.parse_date("bogus") is None
    assert pge_api.parse_date(None) is None


def test_meter_owner_prefers_active_contract():
    accounts = pge_api.parse_billing_accounts(fixture_json("billing_accounts_two_contracts.json"))
    assert pge_api.resolve_meter_owner("01234567", accounts, {}) == (False, "ended", "account_list")
    owner, status, _ = pge_api.resolve_meter_owner("001234567", accounts, {})
    assert owner and status == "active"


def test_meter_owner_without_flags_uses_newest_period():
    accounts = pge_api.parse_billing_accounts(fixture_json("billing_accounts_no_flags.json"))
    for acc in accounts:
        acc.ppe_numbers = ["PL0000000000000000000001"]
    periods = {"1234567": date(2026, 5, 19), "1234568": date(2026, 8, 31)}
    assert pge_api.resolve_meter_owner("1234567", accounts, periods) == (False, "ended", "successor_heuristic")
    assert pge_api.resolve_meter_owner("1234568", accounts, periods)[0] is True
