"""Sensor platform for the PGE Sensor integration.

unique_id of every pre-existing entity is unchanged: ``<slug>_<key>`` where
``slug = slugify(f"{username}_{account_id}")`` (or ``slugify(username)``).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .api import BalanceInfo
from .const import CONF_ACCOUNT_ID, DOMAIN
from .coordinator import PgeEbokCoordinator

MONETARY_UNIT = "PLN"


def entry_slug(username: str, account_id: Any) -> str:
    """Prefix of all unique IDs. Must stay byte-identical to v1.5.1."""
    return slugify(f"{username}_{account_id}") if account_id is not None else slugify(username)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PgeEbokCoordinator = hass.data[DOMAIN][entry.entry_id]
    username = entry.data[CONF_USERNAME]
    # entry.data keeps the account id exactly as stored by v1.5.x
    slug = entry_slug(username, entry.data.get(CONF_ACCOUNT_ID))
    device_name = entry.title or "PGE Sensor"

    sensor_classes = (
        PgeBalanceSensor,
        PgeInvoiceAmountSensor,
        PgeConsumedEnergySensor,
        PgeFeedInEnergySensor,
        PgeSettledEnergySensor,
        PgeEnergyStorageSensor,
        PgeBillingPeriodSensor,
        PgeCurrentPaymentSensor,
        PgeDueDateSensor,
        PgeInvoicesSensor,
        PgeLastSyncSensor,
    )
    async_add_entities(cls(coordinator, slug, username, device_name) for cls in sensor_classes)


class PgeBaseSensor(CoordinatorEntity[PgeEbokCoordinator], SensorEntity):
    """Common behaviour.

    * unavailable when the last refresh failed (no stale values, no zeros);
    * ``contract_status`` and ``last_successful_sync`` on every entity.
    """

    _key: str = ""
    _meter_value = False  # kWh values: only the contract that owns the meter publishes them

    def __init__(
        self,
        coordinator: PgeEbokCoordinator,
        slug: str,
        username: str,
        device_name: str = "PGE Sensor",
    ) -> None:
        super().__init__(coordinator)
        self._slug = slug
        self._username = username
        self._device_name = device_name

    @property
    def unique_id(self) -> str:
        return f"{self._slug}_{self._key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._slug)},
            name=self._device_name,
            manufacturer="PGE",
        )

    @property
    def data(self) -> BalanceInfo | None:
        return self.coordinator.data

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.data is not None

    @property
    def meter_suppressed(self) -> bool:
        """True on an ended/superseded contract: its meter values are not published."""
        return self._meter_value and self.data is not None and not self.data.meter_owner

    def _value(self) -> Any:
        return None

    @property
    def native_value(self) -> Any:
        if self.data is None or self.meter_suppressed:
            return None
        return self._value()

    def _extra(self) -> dict[str, Any]:
        return {}

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.data is None:
            return None
        attrs: dict[str, Any] = {"contract_status": self.data.contract_status}
        if self._meter_value:
            attrs["meter_owner"] = self.data.meter_owner
        if self.coordinator.last_successful_sync:
            attrs["last_successful_sync"] = self.coordinator.last_successful_sync.isoformat()
        attrs.update({k: v for k, v in self._extra().items() if v is not None})
        return attrs


def _round(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


class PgeBalanceSensor(PgeBaseSensor):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = MONETARY_UNIT
    _attr_name = "PGE Saldo"
    _key = "balance"

    def _value(self) -> float | None:
        return _round(self.data.amount)

    def _extra(self) -> dict[str, Any]:
        data = self.data
        return {
            "invoice_number": data.invoice_number,
            "issue_date": data.issue_date.isoformat() if data.issue_date else None,
            "paid_date": data.paid_date.isoformat() if data.paid_date else None,
            "payment_status": data.payment_status,
            "ppe": data.ppe,
        }


class PgeDueDateSensor(PgeBaseSensor):
    _attr_device_class = SensorDeviceClass.DATE
    _attr_name = "PGE Termin płatności"
    _key = "due_date"

    def _value(self) -> date | None:
        return self.data.due_date


class PgeInvoiceAmountSensor(PgeBaseSensor):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = MONETARY_UNIT
    _attr_name = "PGE Kwota faktury"
    _key = "invoice_amount"

    def _value(self) -> float | None:
        return _round(self.data.invoice_amount)


class PgeCurrentPaymentSensor(PgeBaseSensor):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = MONETARY_UNIT
    _attr_name = "PGE Bieżąca płatność"
    _key = "current_payment"

    def _value(self) -> float | None:
        return _round(self.data.current_payment)


class _PeriodEnergySensor(PgeBaseSensor):
    """Energy of the latest settled billing period.

    Not a cumulative meter: each invoice is a new period.  ``TOTAL`` with
    ``last_reset`` = period start makes long-term statistics add every
    period exactly once (``TOTAL_INCREASING`` counted only the difference
    when a period was larger than the previous one).
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.TOTAL
    _meter_value = True
    _field = ""

    def _value(self) -> float | None:
        if self.data.billing_period_start is None:
            return None  # without a period start the value cannot be placed in statistics
        return getattr(self.data, self._field)

    @property
    def last_reset(self) -> datetime | None:
        if self.data is None or self.data.billing_period_start is None:
            return None
        start = self.data.billing_period_start
        return dt_util.start_of_local_day(start)

    def _extra(self) -> dict[str, Any]:
        return {"billing_period": self.data.billing_period}


class PgeConsumedEnergySensor(_PeriodEnergySensor):
    _attr_name = "PGE Energia pobrana"
    _key = "consumed_energy"
    _field = "consumed_energy"


class PgeFeedInEnergySensor(_PeriodEnergySensor):
    _attr_name = "PGE Energia wprowadzona"
    _key = "feed_in_energy"
    _field = "feed_in_energy"


class PgeSettledEnergySensor(PgeBaseSensor):
    # TODO(ACP-57): community code reports this API field is PLN, not kWh.
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_name = "PGE Zużycie po rozliczeniu"
    _key = "settled_energy"
    _meter_value = True

    def _value(self) -> float | None:
        return self.data.settled_energy


def _storage_zone(storage: dict | None) -> dict:
    if not isinstance(storage, dict):
        return {}
    zones = storage.get("zones") or {}
    if not isinstance(zones, dict) or not zones:
        return {}
    return zones.get("Strefa 1") or zones.get("Strefa 1 w sumie") or next(iter(zones.values()), {}) or {}


class PgeEnergyStorageSensor(PgeBaseSensor):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_name = "PGE Magazyn energii"
    _key = "energy_storage"
    _meter_value = True

    def _value(self) -> float | None:
        summary = _storage_zone(self.data.energy_storage).get("dataWarehouseSummary") or {}
        return summary.get("leftEnergyAmountSum")

    def _extra(self) -> dict[str, Any]:
        zone = _storage_zone(self.data.energy_storage)
        summary = zone.get("dataWarehouseSummary") or {}
        history = zone.get("dataWarehousePpm")
        return {
            "settled_energy_sum_with_factor": summary.get("settledEnergyAmountSumWithFactor"),
            "settled_energy_sum": summary.get("settledEnergyAmountSum"),
            "factor": summary.get("factor"),
            "history": sorted(history, key=lambda x: x.get("insertToGridDate", ""), reverse=True)
            if isinstance(history, list)
            else None,
        }


class PgeBillingPeriodSensor(PgeBaseSensor):
    _attr_icon = "mdi:calendar-range"
    _attr_name = "PGE Okres rozliczeniowy"
    _key = "billing_period"

    def _value(self) -> str | None:
        return self.data.billing_period


class PgeInvoicesSensor(PgeBaseSensor):
    """Number of documents of this contract; history in attributes."""

    _attr_icon = "mdi:file-document-multiple"
    _attr_name = "PGE Faktury"
    _key = "invoices"
    # Lists change rarely but are large: keep them out of the recorder.
    _unrecorded_attributes = frozenset({"invoices", "combined_invoices"})

    def _value(self) -> int:
        return len(self.data.invoices)

    def _extra(self) -> dict[str, Any]:
        data = self.data
        last = next((inv for inv in data.invoices if inv.document_type in (None, "INVOICE")), None)
        last = last or (data.invoices[0] if data.invoices else None)
        attrs: dict[str, Any] = {
            "last_invoice": last.as_attribute() if last else None,
            "invoices": [inv.as_attribute() for inv in data.invoices],
        }
        if data.combined_invoices:
            attrs["combined_invoices"] = [inv.as_attribute() for inv in data.combined_invoices]
        return attrs


class PgeLastSyncSensor(PgeBaseSensor):
    """Diagnostic: time of the last successful download. Stays available on errors."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "PGE Ostatnia synchronizacja"
    _key = "last_successful_sync"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_successful_sync

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "last_update_success": self.coordinator.last_update_success,
            "last_error": self.coordinator.last_error,
            "consecutive_failures": self.coordinator.consecutive_failures,
            "pge_support_id": self.coordinator.last_support_id,
            "next_retry_in": str(self.coordinator.update_interval),
        }
