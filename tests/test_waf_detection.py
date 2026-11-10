"""WAF / bot-defense answers must never look like data or like a wrong password."""
from conftest import fixture_text, pge_api


def test_challenge_page_with_http_200_is_a_block():
    assert pge_api.detect_block(200, "text/html", fixture_text("waf_challenge.html")) == "11112222333344445555"


def test_classic_rejected_page_is_a_block():
    assert pge_api.detect_block(200, "text/html; charset=utf-8", fixture_text("waf_rejected.html")) == "99998888777766665555"


def test_support_id_json_403_is_a_block():
    assert pge_api.detect_block(403, "application/json; charset=utf-8", fixture_text("waf_403.json")) == "11112222333344445555"


def test_html_without_content_type_is_still_detected():
    assert pge_api.detect_block(200, "", fixture_text("waf_challenge.html")) is not None


def test_normal_api_answers_are_not_blocks():
    for name in ("login_ok.json", "balance.json", "documents_new_contract.json", "login_bad_credentials.json"):
        assert pge_api.detect_block(200, "application/json", fixture_text(name)) is None


def test_unrelated_html_is_not_a_block():
    assert pge_api.detect_block(200, "text/html", "<html><body>Przerwa techniczna</body></html>") is None
