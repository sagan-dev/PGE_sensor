"""Web eBOK (PrimeFaces) parsers on synthetic HTML."""
import importlib.util
import pathlib
from datetime import date

import pytest

pytest.importorskip("bs4")

from conftest import ROOT, fixture_text  # noqa: E402

_spec = importlib.util.spec_from_file_location("pge_ebok_html", ROOT / "custom_components" / "pge_sensor" / "ebok_html.py")
ebok = importlib.util.module_from_spec(_spec)
import sys  # noqa: E402
sys.modules["pge_ebok_html"] = ebok
_spec.loader.exec_module(ebok)


def test_invoice_table_rows_paid_flag_and_amounts():
    rows = ebok.parse_invoice_table(fixture_text("ebok_finanse_synthetic.html"))
    assert [r.row_index for r in rows] == [0, 1, 2]
    first, second, third = rows
    assert first.due_date == date(2026, 11, 20)
    assert first.amount == 1234.56 and first.amount_to_pay == 1234.56
    assert first.paid is False
    assert first.number == "TEST/0001/2026"
    assert first.consumption_kwh == 321.0
    assert first.issue_date == date(2026, 11, 5)
    assert first.status == "Nieopłacona"
    assert second.paid is True and second.amount_to_pay == 0.0
    # missing values stay None, never 0
    assert third.amount is None and third.amount_to_pay is None and third.consumption_kwh is None


def test_partial_response_details():
    details = ebok.parse_invoice_details(fixture_text("ebok_row_details_partial.xml"))
    assert details == {
        "number": "TEST/0002/2026",
        "consumption_kwh": 1024.5,
        "document_type": "Prognoza",
        "issue_date": date(2026, 9, 1),
        "status": "Opłacona",
    }


def test_missing_table_raises():
    with pytest.raises(ValueError):
        ebok.parse_invoice_table("<html><body>Brak danych</body></html>")


def test_waf_page_is_not_parsed_as_invoices():
    with pytest.raises(ValueError):
        ebok.parse_invoice_table(fixture_text("waf_challenge.html"))


@pytest.mark.parametrize("text,value", [("1 234,56 zł", 1234.56), ("0,00 zł", 0.0), ("brak", None), ("", None), ("-12,5", -12.5)])
def test_pl_amounts(text, value):
    assert ebok.parse_pl_amount(text) == value
