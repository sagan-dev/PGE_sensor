"""Entities, unique_id stability, availability, diagnostics."""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pge_sensor.api import BalanceInfo, Invoice, PgeBlockedError
from custom_components.pge_sensor.const import DOMAIN
from custom_components.pge_sensor.diagnostics import async_get_config_entry_diagnostics

USER = "user@example.invalid"
LEGACY_KEYS = {
    "balance", "due_date", "invoice_amount", "consumed_energy", "feed_in_energy",
    "settled_energy", "energy_storage", "billing_period", "current_payment",
}


def sample(owner: bool = True) -> BalanceInfo:
    inv = Invoice("TEST/FV/0001", "INVOICE", date(2026, 9, 5), date(2026, 9, 25), None,
                  date(2026, 5, 20), date(2026, 8, 31), 222.22, 0.0, "PAID", "001234567")
    return BalanceInfo(
        amount=12.34, due_date=date(2026, 9, 25), invoice_amount=222.22, current_payment=0.0,
        consumed_energy=321.0, feed_in_energy=None, billing_period="20.05.2026 - 31.08.2026",
        billing_period_start=date(2026, 5, 20), billing_period_end=date(2026, 8, 31),
        contract_status="active" if owner else "ended", meter_owner=owner, invoices=[inv],
    )


class FakeClient:
    def __init__(self, *_args, **_kwargs):
        self.results: list = []

    def get_balance_details(self, account_id=None, ppe=None):
        item = FakeClient.queue.pop(0) if len(FakeClient.queue) > 1 else FakeClient.queue[0]
        if isinstance(item, Exception):
            raise item
        return item


async def setup_entry(hass, account_id="01234567", title="Synthetic"):
    entry = MockConfigEntry(
        domain=DOMAIN, title=title, unique_id=f"{USER}_{account_id}",
        data={"username": USER, "password": "not-a-real-password", "account_id": account_id},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.pge_sensor.PgeScraper", FakeClient):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_unique_ids_unchanged(hass):
    FakeClient.queue = [sample()]
    entry = await setup_entry(hass)
    registry = er.async_get(hass)
    unique_ids = {e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    slug = slugify(f"{USER}_01234567")
    assert {f"{slug}_{k}" for k in LEGACY_KEYS} <= unique_ids
    assert f"{slug}_invoices" in unique_ids and f"{slug}_last_successful_sync" in unique_ids
    # leading zero survives in the slug
    assert "_01234567_balance" in next(u for u in unique_ids if u.endswith("_balance"))


async def test_failed_refresh_makes_values_unavailable(hass):
    FakeClient.queue = [sample(), PgeBlockedError(support_id="1")]
    entry = await setup_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    registry = er.async_get(hass)
    ids = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    balance = next(v for k, v in ids.items() if k.endswith("_balance"))
    last_sync = next(v for k, v in ids.items() if k.endswith("_last_successful_sync"))
    assert hass.states.get(balance).state == "12.34"

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(balance).state == STATE_UNAVAILABLE
    state = hass.states.get(last_sync)
    assert state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
    assert state.attributes["last_error"] == "PgeBlockedError"
    assert coordinator.update_interval.total_seconds() == 3600
    assert USER not in coordinator.name and "1234567" not in coordinator.name


async def test_ended_contract_publishes_no_kwh(hass):
    FakeClient.queue = [sample(owner=False)]
    entry = await setup_entry(hass)
    registry = er.async_get(hass)
    consumed = next(e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
                    if e.unique_id.endswith("_consumed_energy"))
    state = hass.states.get(consumed)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["contract_status"] == "ended"
    assert state.attributes["meter_owner"] is False


async def test_active_contract_kwh_has_last_reset(hass):
    FakeClient.queue = [sample()]
    entry = await setup_entry(hass)
    registry = er.async_get(hass)
    consumed = next(e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
                    if e.unique_id.endswith("_consumed_energy"))
    state = hass.states.get(consumed)
    assert state.state == "321.0"
    assert state.attributes["state_class"] == "total"
    assert state.attributes["last_reset"].startswith("2026-05-20")


async def test_diagnostics_are_redacted(hass):
    FakeClient.queue = [sample()]
    entry = await setup_entry(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dump = json.dumps(diag, default=str)
    for forbidden in (USER, "not-a-real-password", "1234567", "TEST/FV", "222.22", "12.34"):
        assert forbidden not in dump
    assert diag["coordinator"]["last_successful_sync"]
    assert diag["account_id_has_leading_zero"] is True
