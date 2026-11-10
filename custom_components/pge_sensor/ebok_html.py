"""Parsers for HTML of the PGE eBOK web portal (JSF / PrimeFaces).

NOT wired into the integration.  The web portal requires reCAPTCHA and F5
bot defense at login, so the integration cannot fetch these pages on its
own without circumventing those protections (which it must not do).  These
pure parsers exist so that HTML the user obtained himself (e.g. a saved
``finanse.xhtml`` page or a PrimeFaces partial response) can be turned into
invoices, if Michał decides on such an import mode (see RAPORT.md).

Structure as observed in a logged-in session on 2026-11-10 (data masked):
  * invoice table: element whose id ends with ``listaFaktur``; rows
    ``tr[data-ri]``; class ``green_row`` = paid; columns "Płatne do",
    "Kwota faktury", "Do zapłaty";
  * row expansion (AJAX): "Faktura nr", "Zużycie całkowite kWh",
    "Rodzaj dokumentu", "Data wystawienia", "Status".
TODO(ACP-57): column order and labels must be confirmed on real HTML.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime

from bs4 import BeautifulSoup

_AMOUNT = re.compile(r"-?\d[\d\s ]*(?:[.,]\d+)?")


def parse_pl_amount(text: str | None) -> float | None:
    """'1 234,56 zł' -> 1234.56; None when there is no number (never 0)."""
    if not text:
        return None
    match = _AMOUNT.search(text)
    if not match:
        return None
    raw = match.group(0).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_pl_date(text: str | None) -> date | None:
    if not text:
        return None
    match = re.search(r"(\d{2})[.-](\d{2})[.-](\d{4})|(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return None
    try:
        if match.group(1):
            return datetime.strptime(f"{match.group(1)}.{match.group(2)}.{match.group(3)}", "%d.%m.%Y").date()
        return date(int(match.group(4)), int(match.group(5)), int(match.group(6)))
    except ValueError:
        return None


@dataclass
class WebInvoice:
    row_index: int
    due_date: date | None
    amount: float | None
    amount_to_pay: float | None
    paid: bool
    number: str | None = None
    consumption_kwh: float | None = None
    document_type: str | None = None
    issue_date: date | None = None
    status: str | None = None


def unwrap_partial_response(text: str) -> str:
    """Concatenate HTML of all ``<update>`` elements of a PrimeFaces partial response."""
    stripped = text.lstrip()
    if not stripped.startswith("<?xml") and not stripped.startswith("<partial-response"):
        return text
    root = ET.fromstring(stripped.encode())
    return "".join(update.text or "" for update in root.iter("update"))


def _cell_texts(row) -> list[str]:
    return [" ".join(td.get_text(" ", strip=True).split()) for td in row.find_all("td", recursive=False)]


def parse_invoice_table(html: str) -> list[WebInvoice]:
    """Rows of the ``[id$=listaFaktur]`` table. Raises ValueError if the table is missing."""
    soup = BeautifulSoup(unwrap_partial_response(html), "html.parser")
    table = soup.find(id=re.compile(r"listaFaktur$")) or soup.find(id=re.compile(r"listaFaktur_data$"))
    if table is None:
        raise ValueError("invoice table not found")
    invoices = []
    for row in table.find_all("tr", attrs={"data-ri": True}):
        cells = [c for c in _cell_texts(row) if c]
        # TODO(ACP-57): confirm order: [Płatne do, Kwota faktury, Do zapłaty]
        due = next((parse_pl_date(c) for c in cells if parse_pl_date(c)), None)
        amounts = [parse_pl_amount(c) for c in cells if parse_pl_date(c) is None and parse_pl_amount(c) is not None]
        invoices.append(
            WebInvoice(
                row_index=int(row["data-ri"]),
                due_date=due,
                amount=amounts[0] if amounts else None,
                amount_to_pay=amounts[1] if len(amounts) > 1 else None,
                paid="green_row" in (row.get("class") or []),
            )
        )
        details = row.find_next_sibling("tr")
        if details is not None and not details.has_attr("data-ri"):
            apply_details(invoices[-1], str(details))
    return invoices


_DETAIL_LABELS = {
    "faktura nr": "number",
    "zużycie całkowite": "consumption_kwh",
    "rodzaj dokumentu": "document_type",
    "data wystawienia": "issue_date",
    "status": "status",
}


def parse_invoice_details(html: str) -> dict[str, object]:
    """Label/value pairs of an expanded invoice row."""
    soup = BeautifulSoup(unwrap_partial_response(html), "html.parser")
    texts = [" ".join(t.split()) for t in soup.stripped_strings]
    result: dict[str, object] = {}
    for i, text in enumerate(texts):
        label = text.rstrip(":").strip().lower()
        for prefix, key in _DETAIL_LABELS.items():
            if label.startswith(prefix) and key not in result:
                value = text.split(":", 1)[1].strip() if ":" in text and text.split(":", 1)[1].strip() else (
                    texts[i + 1] if i + 1 < len(texts) else "")
                if key == "consumption_kwh":
                    result[key] = parse_pl_amount(value)
                elif key == "issue_date":
                    result[key] = parse_pl_date(value)
                else:
                    result[key] = value or None
    return result


def apply_details(invoice: WebInvoice, html: str) -> WebInvoice:
    for key, value in parse_invoice_details(html).items():
        setattr(invoice, key, value)
    return invoice
