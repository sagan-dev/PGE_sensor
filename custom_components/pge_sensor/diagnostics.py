"""Diagnostics for PGE Sensor: state of the sync, without personal data."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .api import BalanceInfo, number_key
from .const import CONF_ACCOUNT_ID, CONF_PPE, DOMAIN
from .coordinator import PgeEbokCoordinator

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, CONF_ACCOUNT_ID, CONF_PPE, "title", "unique_id"}


def summarize(data: BalanceInfo | None) -> dict[str, Any] | None:
    """Shape of the data only: which fields are known, no values, no amounts."""
    if data is None:
        return None
    known = {
        name: getattr(data, name) is not None
        for name in (
            "amount", "due_date", "invoice_number", "issue_date", "paid_date", "payment_status", "ppe",
            "consumed_energy", "feed_in_energy", "settled_energy", "energy_storage", "invoice_amount",
            "billing_period", "current_payment",
        )
    }
    return {
        "fields_known": known,
        "contract_status": data.contract_status,
        "contract_status_source": data.contract_status_source,
        "meter_owner": data.meter_owner,
        "invoice_count": len(data.invoices),
        "combined_invoice_count": len(data.combined_invoices),
        "document_types": sorted({inv.document_type or "?" for inv in data.invoices}),
        "invoice_statuses": sorted({inv.status or "?" for inv in data.invoices}),
        "latest_issue_date": data.invoices[0].issue_date.isoformat() if data.invoices and data.invoices[0].issue_date else None,
        "pdf_hint_present": any(inv.pdf_available for inv in data.invoices),
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator: PgeEbokCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    own_key = (entry.data[CONF_USERNAME].strip().lower(), number_key(entry.data.get(CONF_ACCOUNT_ID)))
    duplicates = [
        other.entry_id
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
        and (other.data[CONF_USERNAME].strip().lower(), number_key(other.data.get(CONF_ACCOUNT_ID))) == own_key
    ]
    same_login = [
        other.entry_id
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
        and other.data[CONF_USERNAME].strip().lower() == own_key[0]
    ]
    result: dict[str, Any] = {
        "entry": async_redact_data({"title": entry.title, "data": dict(entry.data), "version": entry.version}, TO_REDACT),
        "account_id_has_leading_zero": str(entry.data.get(CONF_ACCOUNT_ID, "")).startswith("0"),
        "duplicate_of": duplicates,
        "other_entries_same_login": same_login,
    }
    if coordinator is None:
        result["coordinator"] = None
        return result
    result["coordinator"] = {
        "name": coordinator.name,  # hashed, no personal data
        "last_update_success": coordinator.last_update_success,
        "last_successful_sync": coordinator.last_successful_sync.isoformat() if coordinator.last_successful_sync else None,
        "last_error": coordinator.last_error,
        "consecutive_failures": coordinator.consecutive_failures,
        "pge_support_id": coordinator.last_support_id,
        "update_interval": str(coordinator.update_interval),
        "data": summarize(coordinator.data),
    }
    return result
