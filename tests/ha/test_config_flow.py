"""Config flow: WAF block is not 'invalid_auth'; contracts differing by a leading zero are distinct."""
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pge_sensor.api import PgeAuthError, PgeBlockedError
from custom_components.pge_sensor.const import DOMAIN

USER = {"username": "user@example.invalid", "password": "not-a-real-password"}
FETCH = "custom_components.pge_sensor.config_flow._async_fetch_accounts"


async def _submit(hass, side_effect=None, return_value=None):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(FETCH, side_effect=side_effect, return_value=return_value):
        return await hass.config_entries.flow.async_configure(result["flow_id"], USER)


async def test_waf_block_is_cannot_connect(hass):
    result = await _submit(hass, side_effect=PgeBlockedError())
    assert result["errors"] == {"base": "cannot_connect"}


async def test_bad_password_is_invalid_auth(hass):
    result = await _submit(hass, side_effect=PgeAuthError())
    assert result["errors"] == {"base": "invalid_auth"}


async def test_leading_zero_variant_is_offered_as_new_contract(hass):
    MockConfigEntry(domain=DOMAIN, unique_id="user@example.invalid_01234567",
                    data={**USER, "account_id": "01234567"}).add_to_hass(hass)
    accounts = [{"id": "01234567", "archived": True}, {"id": "001234567", "archived": False}]
    result = await _submit(hass, return_value=accounts)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["account_id"] == "001234567"


async def test_same_contract_is_already_configured(hass):
    MockConfigEntry(domain=DOMAIN, unique_id="user@example.invalid_01234567",
                    data={**USER, "account_id": "01234567"}).add_to_hass(hass)
    result = await _submit(hass, return_value=[{"id": "01234567"}])
    assert result["type"] == FlowResultType.ABORT


async def test_options_mark_contract_ended(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user@example.invalid_01234567",
                            data={**USER, "account_id": "01234567"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"contract_ended": True})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options == {"contract_ended": True}
