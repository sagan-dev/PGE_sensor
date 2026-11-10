"""Login / session flow against a fake HTTP session."""
import pytest
import requests

from conftest import FakeResponse, fixture_text, make_client, ok, pge_api

LOGIN = "/auth/login"
ACCOUNTS = "/billingAccounts/filter"
BALANCE = "/balance"
DOCS = "/documents/filter"
DW = "/datawarehouse/"


def full_session(session, docs="documents_new_contract.json"):
    session.add("POST", LOGIN, ok("login_ok.json"))
    session.add("POST", ACCOUNTS, ok("billing_accounts_two_contracts.json"))
    session.add("GET", BALANCE, ok("balance.json"))
    session.add("POST", "/billingAccounts/01234567/documents", ok("documents_old_contract.json"))
    session.add("POST", DOCS, ok(docs))
    session.add("GET", DW, ok("datawarehouse.json"))
    return session


def test_waf_challenge_on_login_is_blocked_error_not_auth(session):
    session.add("POST", LOGIN, FakeResponse(200, fixture_text("waf_challenge.html"), "text/html"))
    client = make_client(session)
    with pytest.raises(pge_api.PgeBlockedError) as err:
        client.get_balance_details("001234567")
    assert err.value.support_id == "11112222333344445555"
    assert not isinstance(err.value, pge_api.PgeAuthError)


def test_waf_403_on_login_is_blocked_error(session):
    session.add("POST", LOGIN, FakeResponse(403, fixture_text("waf_403.json")))
    with pytest.raises(pge_api.PgeBlockedError):
        make_client(session).get_billing_accounts()


def test_connection_reset_is_connection_error(session):
    session.add("POST", LOGIN, requests.exceptions.ConnectionError("reset"))
    with pytest.raises(pge_api.PgeConnectionError):
        make_client(session).get_billing_accounts()


def test_bad_credentials_are_auth_error(session):
    session.add("POST", LOGIN, FakeResponse(400, fixture_text("login_bad_credentials.json")))
    with pytest.raises(pge_api.PgeAuthError):
        make_client(session).get_billing_accounts()


def test_non_json_login_answer_is_api_error(session):
    session.add("POST", LOGIN, FakeResponse(200, "OK", "text/plain"))
    with pytest.raises(pge_api.PgeApiError):
        make_client(session).get_billing_accounts()


def test_token_is_reused_between_refreshes(session):
    client = make_client(full_session(session))
    client.cache_ttl = 0  # force fresh data on every call
    client.get_balance_details("001234567")
    client.get_balance_details("001234567")
    assert session.count(LOGIN) == 1
    auth_headers = [h.get("Authorization") for m, u, h in session.calls if LOGIN not in u]
    assert all(h and h.startswith("Bearer ") for h in auth_headers)
    assert all("Authorization" not in h for m, u, h in session.calls if LOGIN in u)


def test_expired_token_triggers_single_relogin(session):
    full_session(session)
    session.routes.insert(0, ("GET", BALANCE, [FakeResponse(401, "{}"), ok("balance.json")]))
    client = make_client(session)
    info = client.get_balance_details("001234567")
    assert info.amount == 123.45
    assert session.count(LOGIN) == 2


def test_full_refresh_new_contract_owns_meter(session):
    client = make_client(full_session(session))
    info = client.get_balance_details("001234567")
    assert info.meter_owner is True
    assert info.contract_status == "active"
    assert info.energy_storage["zones"]["Strefa 1"]["dataWarehouseSummary"]["leftEnergyAmountSum"] == 10.5
    contracts = {i.account_key for i in info.combined_invoices}
    assert contracts == {"001234567", "01234567"}
    # URL uses the number exactly as PGE lists it (leading zeros kept)
    assert any("/billingAccounts/001234567/balance" in u for _, u, _ in session.calls)


def test_old_contract_is_ended_and_kept_apart_from_successor(session):
    """Old contract 01234567 and successor 001234567 are not merged."""
    full_session(session, docs="documents_old_contract.json")
    info = make_client(session).get_balance_details("01234567")
    assert any("/billingAccounts/01234567/balance" in u for _, u, _ in session.calls)
    assert not any("/billingAccounts/001234567/balance" in u for _, u, _ in session.calls)
    assert info.meter_owner is False
    assert info.contract_status == "ended"
    assert {i.account_key for i in info.combined_invoices} == {"01234567", "001234567"}


def test_legacy_int_id_ambiguous_uses_stored_value(session):
    """Stored int 1234567 while both zero-variants exist: no silent re-mapping."""
    full_session(session)
    make_client(session).get_balance_details(1234567)
    assert any("/billingAccounts/1234567/balance" in u for _, u, _ in session.calls)


def test_documents_failure_raises_instead_of_empty_data(session):
    session.add("POST", LOGIN, ok("login_ok.json"))
    session.add("POST", ACCOUNTS, ok("billing_accounts_two_contracts.json"))
    session.add("GET", BALANCE, ok("balance.json"))
    session.add("POST", DOCS, FakeResponse(503, "busy", "text/plain"))
    with pytest.raises(pge_api.PgeConnectionError):
        make_client(session).get_balance_details("001234567")


def test_no_personal_data_in_error_messages(session):
    session.add("POST", LOGIN, FakeResponse(200, fixture_text("waf_challenge.html"), "text/html"))
    with pytest.raises(pge_api.PgeScraperError) as err:
        make_client(session).get_balance_details("001234567")
    text = str(err.value)
    assert "example.invalid" not in text and "001234567" not in text and "password" not in text


def test_no_personal_data_in_logs(session, caplog):
    caplog.set_level("DEBUG")
    client = make_client(full_session(session))
    client.get_balance_details("001234567")
    session.routes.insert(0, ("POST", LOGIN, [FakeResponse(200, fixture_text("waf_challenge.html"), "text/html")]))
    client._access_token = None
    with pytest.raises(pge_api.PgeBlockedError):
        client.get_balance_details("001234567")
    for forbidden in ("example.invalid", "1234567", "PL0000", "123.45", "not-a-real-password", "eyJ"):
        assert forbidden not in caplog.text
