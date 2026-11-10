"""PGE Sensor API client for the mBOK PGE mobile JSON API.

The integration never scraped HTML: it talks to the JSON API used by the
mobile application (``mbok-services.gkpge.pl``).  Since 2026-11-06 the PGE
hosts answer automated clients through an F5 BIG-IP WAF with bot defense:
a challenge page (HTML, often HTTP 200), a JSON ``{"supportID": ...}``
rejection (HTTP 403) or a TCP reset.  Earlier versions reported all of that
as "Login request failed" (or, for 403, as wrong credentials).

This module:
  * recognises WAF/bot-defense answers and raises ``PgeBlockedError``
    (never "invalid credentials", never data);
  * reuses the access token until it expires or the API answers 401,
    instead of logging in on every refresh;
  * keeps parsers pure (no I/O) so they can be tested on fixtures;
  * never returns 0 for a value it did not receive.

It deliberately does NOT try to solve or evade the bot defense (no CAPTCHA
solving, no browser emulation, no fingerprint spoofing).

Field names of API responses come from the previous implementation and from
public community code.  Places marked ``TODO(ACP-57)`` still need
confirmation against a real, logged-in session.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional

import requests

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://mbok-services.gkpge.pl"
AUTH_URL = f"{BASE_URL}/authorization-service/api/auth/login"
BILLING_ACCOUNTS_URL = f"{BASE_URL}/billing-service-v1/billingAccounts/filter"
BALANCE_URL = BASE_URL + "/billing-service-v1/billingAccounts/{account_id}/balance"
DOCUMENTS_URL = (
    BASE_URL + "/mbok-service-v1/billingAccounts/{account_id}/documents/filter?pageNo=0&pageSize={page_size}"
)
DATAWAREHOUSE_URL = BASE_URL + "/mbok-service-v1/billingAccountsDocument/{document_id}/datawarehouse/{ppm_id}"

DOCUMENTS_PAGE_SIZE = 50
INVOICE_HISTORY_LIMIT = 24
TOKEN_REFRESH_MARGIN = 120  # seconds before JWT expiry when we log in again
DEFAULT_TOKEN_LIFETIME = 15 * 60  # used when the token is not a readable JWT

# documentType values seen in public community code. TODO(ACP-57): confirm.
DOC_TYPE_INVOICE = "INVOICE"
DOC_TYPE_FORECAST = "PREPAYMENT_INVOICE"

CONTRACT_ACTIVE = "active"
CONTRACT_ENDED = "ended"
CONTRACT_UNKNOWN = "unknown"

_ENDED_STATUSES = {"ARCHIVED", "ARCHIWALNE", "INACTIVE", "CLOSED", "TERMINATED", "ENDED", "ZAKONCZONA"}
_ACTIVE_STATUSES = {"ACTIVE", "AKTYWNA", "AKTYWNE", "OPEN"}

_WAF_HTML_MARKERS = (
    "/tspd/",
    "support id",
    "request rejected",
    "the requested url was rejected",
    "czy jestes czlowiekiem",
    "czy jesteś człowiekiem",
    "captcha",
)


# --------------------------------------------------------------------------- errors


class PgeScraperError(RuntimeError):
    """Base error of the PGE API client. Messages never contain personal data."""


class PgeAuthError(PgeScraperError):
    """The API explicitly rejected the credentials or the token."""

    def __init__(self, message: str = "Login failed: incorrect credentials") -> None:
        super().__init__(message)


class PgeBlockedError(PgeScraperError):
    """The request was stopped by the PGE WAF / bot defense, not by the API."""

    def __init__(self, message: str = "Request blocked by PGE WAF/bot defense", support_id: str | None = None) -> None:
        super().__init__(message)
        # A support ID is not personal data; PGE needs it to investigate a block.
        self.support_id = support_id


class PgeConnectionError(PgeScraperError):
    """Network problem (DNS, TLS, timeout, reset) or HTTP 5xx from PGE."""


class PgeApiError(PgeScraperError):
    """Unexpected answer from the API (format change, missing fields, 4xx)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------- helpers


def normalize_number(value: Any) -> str | None:
    """Customer/account/PPE number as a string, keeping leading zeros.

    PGE numbers must never go through ``int()``: the same customer may be
    shown as ``01234567`` and ``001234567``.  Whitespace and common
    separators are removed, everything else is kept verbatim.
    """
    if value is None or isinstance(value, bool):
        return None
    text = re.sub(r"[\s\-/.]", "", str(value).strip())
    return text or None


def number_key(value: Any) -> str | None:
    """Comparison key: normalized and without leading zeros.

    ``number_key("01234567") == number_key("001234567") == number_key(1234567)``.
    Only for comparisons, never for display or URLs.
    """
    text = normalize_number(value)
    if text is None:
        return None
    if text.isdigit():
        return text.lstrip("0") or "0"
    return text.upper()


def same_number(left: Any, right: Any) -> bool:
    lkey = number_key(left)
    return lkey is not None and lkey == number_key(right)


def numeric(value: Any) -> float | None:
    """Convert API amounts to float; ``None`` (never 0) when missing or invalid."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def parse_dt(value: Any) -> datetime | None:
    """Parse API timestamps such as ``2026-05-20T00:00:00.000+0000``."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("T", " ")
    text = re.split(r"[.+Z]", text, maxsplit=1)[0].strip()
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value.strip()):
        text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_date(value: Any) -> date | None:
    parsed = parse_dt(value)
    return parsed.date() if parsed else None


def _first(mapping: dict, *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


# --------------------------------------------------------------------------- WAF detection


def detect_block(status_code: int, content_type: str, body: str) -> str | None:
    """Recognise an answer of the WAF / bot defense instead of the API.

    Returns the support ID when readable, ``""`` for a block without a
    readable ID and ``None`` for a normal API answer.  Pure function.
    """
    text = body or ""
    stripped = text.lstrip()
    ctype = (content_type or "").lower()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict) and "supportID" in data and len(data) <= 2:
            return str(data.get("supportID") or "")
        return None
    is_html = "html" in ctype or stripped[:15].lower().startswith(("<!doctype html", "<html"))
    if not is_html:
        return None
    lowered = text.lower()
    if not any(marker in lowered for marker in _WAF_HTML_MARKERS):
        return None
    support = re.search(r"support id is:?\s*(\d{6,})", text, re.IGNORECASE) or re.search(
        r"Komunikat b[lł][eę]du:\s*(\d{6,})", text
    )
    return support.group(1) if support else ""


def jwt_expiry(token: str) -> float | None:
    """Read ``exp`` from a JWT without verifying it (only to schedule re-login)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload.encode())).get("exp")
        return float(exp) if exp else None
    except Exception:  # noqa: BLE001 - opaque token, fall back to default lifetime
        return None


# --------------------------------------------------------------------------- data model


@dataclass
class Invoice:
    """One document from an account's document list."""

    number: str | None
    document_type: str | None
    issue_date: date | None
    due_date: date | None
    paid_date: date | None
    period_from: date | None
    period_to: date | None
    amount: float | None
    amount_to_pay: float | None
    status: str | None
    account_key: str | None = None  # contract (billing account) the document belongs to
    pdf_available: bool = False

    def as_attribute(self, contract_label: str | None = None) -> dict[str, Any]:
        return {
            "number": self.number,
            "type": self.document_type,
            "issue_date": self.issue_date.isoformat() if self.issue_date else None,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "paid_date": self.paid_date.isoformat() if self.paid_date else None,
            "period_from": self.period_from.isoformat() if self.period_from else None,
            "period_to": self.period_to.isoformat() if self.period_to else None,
            "amount": self.amount,
            "amount_to_pay": self.amount_to_pay,
            "status": self.status,
            "contract": contract_label if contract_label is not None else self.account_key,
        }


@dataclass
class AccountInfo:
    """One billing account (contract) from ``billingAccounts/filter``."""

    id: str
    label: str
    ppe_numbers: list[str]
    contract_status: str
    default: bool = False


@dataclass
class BalanceInfo:
    """Data of one billing account. ``None`` always means "not known"."""

    amount: Optional[float] = None
    due_date: Optional[date] = None
    invoice_number: Optional[str] = None
    issue_date: Optional[date] = None
    paid_date: Optional[date] = None
    payment_status: Optional[str] = None
    ppe: Optional[str] = None
    consumed_energy: Optional[float] = None
    feed_in_energy: float | None = None
    settled_energy: float | None = None
    energy_storage: dict | None = None
    invoice_amount: Optional[float] = None
    billing_period: Optional[str] = None
    billing_period_start: Optional[date] = None
    billing_period_end: Optional[date] = None
    current_payment: Optional[float] = None
    contract_status: str = CONTRACT_UNKNOWN
    contract_status_source: str | None = None
    meter_owner: bool = True
    invoices: list[Invoice] = field(default_factory=list)
    combined_invoices: list[Invoice] = field(default_factory=list)


# --------------------------------------------------------------------------- parsers


def parse_login_response(data: Any) -> str:
    if not isinstance(data, dict):
        raise PgeApiError("Login response is not a JSON object")
    token = data.get("accessToken")
    if not token or not isinstance(token, str):
        raise PgeApiError("Login response missing accessToken")
    return token


def account_contract_status(account: dict) -> tuple[str, str | None]:
    """Return ``(status, source)`` of a billing account.

    TODO(ACP-57): ``archived`` comes from public community code; the other
    keys are defensive guesses and must be confirmed on a real session.
    """
    if account.get("archived") is True or account.get("isArchived") is True:
        return CONTRACT_ENDED, "archived_flag"
    status = _first(account, "status", "accountStatus", "contractStatus")
    if isinstance(status, str):
        upper = status.strip().upper()
        if upper in _ENDED_STATUSES:
            return CONTRACT_ENDED, "status_field"
        if upper in _ACTIVE_STATUSES:
            return CONTRACT_ACTIVE, "status_field"
    end = parse_date(_first(account, "contractEndDate", "endDate", "validTo"))
    if end is not None:
        return (CONTRACT_ENDED if end < date.today() else CONTRACT_ACTIVE), "end_date"
    if account.get("archived") is False:
        return CONTRACT_ACTIVE, "archived_flag"
    return CONTRACT_UNKNOWN, None


def account_ppe_numbers(account: dict) -> list[str]:
    numbers: list[str] = []
    for point in account.get("ppms") or []:
        if isinstance(point, dict):
            num = normalize_number(_first(point, "number", "ppmNumber", "ppeNumber"))
            if num:
                numbers.append(num)
    direct = normalize_number(account.get("ppeNumber"))
    if direct:
        numbers.append(direct)
    return list(dict.fromkeys(numbers))


def describe_billing_account(account: dict) -> str:
    """Label used as config entry title (UI only, never logged)."""
    for key in ("ppeNumber", "premiseAddress", "address", "shortName", "name", "consumerNumber", "accountNumber", "number"):
        value = account.get(key)
        if value:
            label = str(value)
            break
    else:
        account_id = account.get("id")
        label = f"Punkt poboru {account_id}" if account_id is not None else "Punkt poboru"
    if account_contract_status(account)[0] == CONTRACT_ENDED:
        label += " (umowa zakończona)"
    return label


def parse_billing_accounts(data: Any) -> list[AccountInfo]:
    if not isinstance(data, dict) or not isinstance(data.get("billingAccounts", []), list):
        raise PgeApiError("Unexpected billing accounts response")
    result = []
    for raw in data.get("billingAccounts", []):
        if not isinstance(raw, dict) or normalize_number(raw.get("id")) is None:
            continue
        result.append(
            AccountInfo(
                id=normalize_number(raw["id"]),
                label=describe_billing_account(raw),
                ppe_numbers=account_ppe_numbers(raw),
                contract_status=account_contract_status(raw)[0],
                default=bool(raw.get("defaultAccount")),
            )
        )
    return result


def _rows(doc: dict) -> list[dict]:
    return [p for p in doc.get("ppmConsumptions") or [] if isinstance(p, dict)]


def _doc_sort_key(doc: dict) -> tuple[datetime, datetime]:
    created = parse_dt(_first(doc, "creationDate", "issueDate")) or datetime.min
    period_to = max((parse_dt(p.get("billingPeriodTo")) or datetime.min for p in _rows(doc)), default=datetime.min)
    return created, period_to


def sort_documents(documents: Iterable[dict]) -> list[dict]:
    """Newest first; the API order is not guaranteed."""
    return sorted((d for d in documents if isinstance(d, dict)), key=_doc_sort_key, reverse=True)


def parse_documents_response(data: Any) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("documents", []), list):
        raise PgeApiError("Unexpected documents response")
    return sort_documents(data.get("documents", []))


def _period_of(doc: dict) -> tuple[date | None, date | None]:
    froms = [d for d in (parse_date(p.get("billingPeriodFrom")) for p in _rows(doc)) if d]
    tos = [d for d in (parse_date(p.get("billingPeriodTo")) for p in _rows(doc)) if d]
    return (min(froms) if froms else None, max(tos) if tos else None)


def document_to_invoice(doc: dict, account_key: str | None = None) -> Invoice:
    period_from, period_to = _period_of(doc)
    return Invoice(
        number=_first(doc, "documentNumber", "number"),
        document_type=doc.get("documentType"),
        issue_date=parse_date(_first(doc, "creationDate", "issueDate")),
        due_date=parse_date(doc.get("paymentDueDate")),
        paid_date=parse_date(doc.get("paidDate")),
        period_from=period_from,
        period_to=period_to,
        amount=numeric(doc.get("totalAmount")),
        amount_to_pay=numeric(doc.get("amountToPay")),
        status=doc.get("paymentStatus"),
        account_key=account_key,
        # TODO(ACP-57): the PDF endpoint is unknown; we only report a hint from the API.
        pdf_available=bool(doc.get("pdfAvailable") or doc.get("fileId")),
    )


def is_unpaid(doc: dict) -> bool:
    to_pay = numeric(doc.get("amountToPay"))
    if to_pay is not None:
        return to_pay > 0
    status = doc.get("paymentStatus")
    return status is not None and status != "PAID"


def latest_settlement(documents: list[dict]) -> dict | None:
    """Newest real invoice with consumption rows (forecasts are skipped)."""
    for doc in sort_documents(documents):
        if doc.get("documentType") not in (None, DOC_TYPE_INVOICE):
            continue
        if _rows(doc):
            return doc
    return None


def _row_ppe(row: dict) -> str | None:
    return normalize_number(row.get("ppmNumber") or (row.get("ppm") or {}).get("ppmNumber"))


def select_consumption(doc: dict, ppe: str | None = None) -> dict | None:
    """Pick the PPE row of a document. Several PPE without a hint: ``None``."""
    rows = _rows(doc)
    if ppe:
        return next((row for row in rows if same_number(_row_ppe(row), ppe)), None)
    return rows[0] if len(rows) == 1 else None


def build_balance_info(
    balance: dict,
    documents: list[dict],
    *,
    account_key: str | None = None,
    ppe: str | None = None,
    energy_storage: dict | None = None,
) -> BalanceInfo:
    """Turn raw API payloads into ``BalanceInfo``. Pure, no I/O."""
    if not isinstance(balance, dict):
        raise PgeApiError("Unexpected balance response")
    docs = sort_documents(documents)
    info = BalanceInfo(amount=numeric(_first(balance, "totalAmount", "requiredAmount")))

    due_dates = sorted(d for d in (parse_date(x.get("paymentDueDate")) for x in docs if is_unpaid(x)) if d)
    info.due_date = due_dates[0] if due_dates else None

    latest = docs[0] if docs else None
    if latest:
        info.invoice_number = latest.get("documentNumber")
        info.payment_status = latest.get("paymentStatus")
        info.invoice_amount = numeric(latest.get("totalAmount"))
        info.current_payment = numeric(latest.get("amountToPay"))
        info.issue_date = parse_date(latest.get("creationDate"))
        info.paid_date = parse_date(latest.get("paidDate"))
        if info.due_date is None:
            info.due_date = parse_date(latest.get("paymentDueDate"))

    settlement = latest_settlement(docs)
    row = select_consumption(settlement, ppe) if settlement else None
    if row:
        start, end = parse_date(row.get("billingPeriodFrom")), parse_date(row.get("billingPeriodTo"))
        if start and end:
            info.billing_period = f"{start:%d.%m.%Y} - {end:%d.%m.%Y}"
        info.billing_period_start, info.billing_period_end = start, end
        info.ppe = _row_ppe(row)
        info.consumed_energy = numeric(row.get("consumptionValue"))
        info.feed_in_energy = numeric(row.get("feedInValue"))
        # TODO(ACP-57): community code says this field is PLN, not kWh. Confirm.
        info.settled_energy = numeric(row.get("valueSettledEnergyConsumed"))

    info.energy_storage = energy_storage if isinstance(energy_storage, dict) else None
    info.invoices = [document_to_invoice(d, account_key) for d in docs[:INVOICE_HISTORY_LIMIT]]
    return info


def resolve_meter_owner(
    own_id: str,
    accounts: list[AccountInfo],
    latest_period_end: dict[str, date | None],
) -> tuple[bool, str, str | None]:
    """Decide whether this contract may publish meter (kWh) values.

    After a contract change one physical meter (PPE) is billed on several
    billing accounts.  Only one of them may feed kWh into HA, otherwise the
    Energy dashboard gets duplicated series.  Order of preference: not ended,
    newest settled period, highest account number.

    Returns ``(is_owner, contract_status, source)``.
    """
    own = next((a for a in accounts if same_number(a.id, own_id)), None)
    if own is None:
        return True, CONTRACT_UNKNOWN, None
    status = own.contract_status
    source = "account_list" if status != CONTRACT_UNKNOWN else None
    if status == CONTRACT_ENDED:
        return False, CONTRACT_ENDED, source

    own_ppes = {number_key(p) for p in own.ppe_numbers}
    siblings = [
        a for a in accounts if not same_number(a.id, own_id) and own_ppes & {number_key(p) for p in a.ppe_numbers}
    ]
    if not siblings:
        return True, status, source

    def rank(acc: AccountInfo) -> tuple[int, date, str]:
        return (
            0 if acc.contract_status == CONTRACT_ENDED else 1,
            latest_period_end.get(acc.id) or date.min,
            (number_key(acc.id) or "").zfill(32),
        )

    owner = max([own, *siblings], key=rank)
    if same_number(owner.id, own.id):
        return True, (status if status != CONTRACT_UNKNOWN else CONTRACT_ACTIVE), source or "successor_heuristic"
    return False, CONTRACT_ENDED, "successor_heuristic"


# --------------------------------------------------------------------------- client


class PgeScraper:
    """Thread-safe API client. One instance is shared by all entries of a login."""

    AUTH_URL = AUTH_URL
    BILLING_ACCOUNTS_URL = BILLING_ACCOUNTS_URL
    BASE_URL = BASE_URL

    def __init__(
        self, username: str, password: str, *, timeout: int = 15, session: requests.Session | None = None
    ) -> None:
        if not username or not password:
            raise ValueError("Username and password must be provided")
        self._username = username
        self._password = password
        self._timeout = timeout
        self._session = session or requests.Session()
        # Same identification as previous releases; no browser emulation.
        self._session.headers.update(
            {"User-Agent": "okhttp/4.9.2", "Content-Type": "application/json", "Accept": "application/json"}
        )
        self._access_token: Optional[str] = None
        self._token_expires = 0.0
        self._device_id = str(uuid.uuid4())
        self._lock = threading.RLock()
        self._documents_cache: dict[str, tuple[float, list[dict]]] = {}
        self._accounts_cache: tuple[float, list[dict]] | None = None
        self.cache_ttl = 600.0
        self.last_support_id: str | None = None

    # ---- low level -------------------------------------------------------

    def _raw(self, method: str, url: str, *, payload: Any = None, auth: bool = True) -> Any:
        headers = {"Authorization": f"Bearer {self._access_token}"} if auth and self._access_token else {}
        try:
            response = self._session.request(method, url, json=payload, headers=headers, timeout=self._timeout)
        except requests.exceptions.SSLError as exc:
            raise PgeConnectionError("TLS error talking to PGE") from exc
        except requests.exceptions.Timeout as exc:
            raise PgeConnectionError("PGE request timed out") from exc
        except requests.exceptions.ConnectionError as exc:
            # The PGE WAF also resets connections of blocked clients.
            raise PgeConnectionError("Connection to PGE failed or was reset") from exc
        except requests.RequestException as exc:
            raise PgeConnectionError("PGE request failed") from exc

        status = response.status_code
        block = detect_block(status, response.headers.get("Content-Type", ""), response.text or "")
        if block is not None:
            self.last_support_id = block or None
            raise PgeBlockedError(f"Request blocked by PGE WAF/bot defense (HTTP {status})", support_id=block or None)
        if status == 401:
            raise PgeAuthError("Unauthorized")
        if status >= 500:
            raise PgeConnectionError(f"PGE server error (HTTP {status})")
        if status >= 400:
            raise PgeApiError(f"Unexpected HTTP {status} from PGE API", status_code=status)
        try:
            return response.json()
        except ValueError as exc:
            raise PgeApiError(f"PGE API returned non-JSON content (HTTP {status})", status_code=status) from exc

    def _login(self) -> None:
        payload = {
            "email": self._username,
            "password": self._password,
            "deviceId": self._device_id,
            "deviceName": "Home Assistant",
            "platform": "Android",
        }
        self._access_token = None
        try:
            data = self._raw("POST", self.AUTH_URL, payload=payload, auth=False)
        except PgeAuthError as exc:
            raise PgeAuthError() from exc
        except PgeApiError as exc:
            # A JSON error from the API itself (not the WAF) on the login call.
            # TODO(ACP-57): confirm which status PGE uses for a wrong password.
            if exc.status_code in (400, 403):
                raise PgeAuthError() from exc
            raise
        token = parse_login_response(data)
        self._access_token = token
        self._token_expires = jwt_expiry(token) or time.time() + DEFAULT_TOKEN_LIFETIME
        _LOGGER.debug("PGE login succeeded")

    def _ensure_login(self) -> None:
        if not self._access_token or time.time() > self._token_expires - TOKEN_REFRESH_MARGIN:
            self._login()

    def _call(self, method: str, url: str, payload: Any = None) -> Any:
        """Authorized call; on 401 log in again exactly once."""
        with self._lock:
            self._ensure_login()
            try:
                return self._raw(method, url, payload=payload)
            except PgeAuthError:
                _LOGGER.debug("PGE token rejected, logging in again")
                self._login()
                return self._raw(method, url, payload=payload)

    # ---- public API ------------------------------------------------------

    def get_billing_accounts(self, *, use_cache: bool = False) -> list[dict]:
        """Raw billing accounts (config flow and contract resolution)."""
        with self._lock:
            if use_cache and self._accounts_cache and time.time() - self._accounts_cache[0] < self.cache_ttl:
                return self._accounts_cache[1]
            data = self._call("POST", self.BILLING_ACCOUNTS_URL, {})
            parse_billing_accounts(data)  # validates the format
            accounts = [a for a in data.get("billingAccounts", []) if isinstance(a, dict)]
            self._accounts_cache = (time.time(), accounts)
            return accounts

    def get_documents(self, account_id: Any, *, use_cache: bool = False) -> list[dict]:
        key = normalize_number(account_id)
        if key is None:
            raise PgeApiError("Invalid billing account id")
        with self._lock:
            cached = self._documents_cache.get(key)
            if use_cache and cached and time.time() - cached[0] < self.cache_ttl:
                return cached[1]
            url = DOCUMENTS_URL.format(account_id=key, page_size=DOCUMENTS_PAGE_SIZE)
            docs = parse_documents_response(self._call("POST", url, {}))
            self._documents_cache[key] = (time.time(), docs)
            return docs

    def _default_account_id(self) -> str:
        accounts = parse_billing_accounts({"billingAccounts": self.get_billing_accounts(use_cache=True)})
        if not accounts:
            raise PgeApiError("No billing accounts returned by PGE")
        return next((a for a in accounts if a.default), accounts[0]).id

    def get_balance_details(self, account_id: Any = None, ppe: str | None = None) -> BalanceInfo:
        """Fetch everything for one billing account. Raises on any failure."""
        with self._lock:
            own_id = normalize_number(account_id) if account_id is not None else self._default_account_id()
            if own_id is None:
                raise PgeApiError("Invalid billing account id")
            # Use the number exactly as PGE spells it today (e.g. with a new
            # leading zero), while matching the stored id without zeros.
            current = next(
                (a.id for a in parse_billing_accounts({"billingAccounts": self.get_billing_accounts(use_cache=True)})
                 if same_number(a.id, own_id)),
                None,
            )
            own_id = current or own_id
            balance = self._call("GET", BALANCE_URL.format(account_id=own_id))
            documents = self.get_documents(own_id)

            storage = None
            settlement = latest_settlement(documents)
            row = select_consumption(settlement, ppe) if settlement else None
            if settlement and row and settlement.get("prosument") is True:
                ppm_id = (row.get("ppm") or {}).get("id")
                document_id = settlement.get("documentId")
                if ppm_id and document_id:
                    try:
                        storage = self._call("GET", DATAWAREHOUSE_URL.format(document_id=document_id, ppm_id=ppm_id))
                    except (PgeApiError, PgeConnectionError) as exc:
                        # Optional data: only the storage sensor becomes unavailable.
                        _LOGGER.debug("Energy storage data unavailable: %s", type(exc).__name__)

            info = build_balance_info(balance, documents, account_key=own_id, ppe=ppe, energy_storage=storage)
            self._apply_contract_view(info, own_id, ppe)
            return info

    def _apply_contract_view(self, info: BalanceInfo, own_id: str, ppe: str | None) -> None:
        """Mark ended contracts and build the combined invoice view of one meter."""
        accounts = parse_billing_accounts({"billingAccounts": self.get_billing_accounts(use_cache=True)})
        own = next((a for a in accounts if same_number(a.id, own_id)), None)
        if own is not None and not own.ppe_numbers and info.ppe:
            own.ppe_numbers = [info.ppe]
        own_ppes = {number_key(p) for p in (own.ppe_numbers if own else [])}
        if info.ppe:
            own_ppes.add(number_key(info.ppe))
        period_end: dict[str, date | None] = {own_id: info.billing_period_end}
        combined = list(info.invoices)
        siblings = []
        for acc in accounts:
            if same_number(acc.id, own_id):
                continue
            # Accounts without PPE in the list are matched by their latest invoice.
            try:
                docs = self.get_documents(acc.id, use_cache=True) if (
                    own_ppes & {number_key(p) for p in acc.ppe_numbers} or not acc.ppe_numbers
                ) else None
            except PgeScraperError as exc:
                _LOGGER.debug("Other contract documents unavailable: %s", type(exc).__name__)
                continue
            if docs is None:
                continue
            settlement = latest_settlement(docs)
            row = select_consumption(settlement, ppe) if settlement else None
            if not acc.ppe_numbers:
                row_ppe = _row_ppe(row) if row else None
                if not row_ppe or number_key(row_ppe) not in own_ppes:
                    continue
                acc.ppe_numbers = [row_ppe]
            siblings.append(acc)
            period_end[acc.id] = parse_date(row.get("billingPeriodTo")) if row else None
            combined.extend(document_to_invoice(d, acc.id) for d in docs[:INVOICE_HISTORY_LIMIT])

        owner, status, source = resolve_meter_owner(own_id, accounts, period_end)
        info.meter_owner = owner
        info.contract_status = status
        info.contract_status_source = source
        if siblings:
            seen: set[tuple] = set()
            unique = []
            for inv in sorted(combined, key=lambda i: i.issue_date or date.min, reverse=True):
                key = (inv.number, inv.account_key)
                if key not in seen:
                    seen.add(key)
                    unique.append(inv)
            info.combined_invoices = unique[: INVOICE_HISTORY_LIMIT * 2]
