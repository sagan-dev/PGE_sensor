"""Config flow for the PGE Sensor integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .api import PgeAuthError, PgeScraper, PgeScraperError, describe_billing_account, number_key
from .const import CONF_ACCOUNT_ID, DOMAIN

ALL_POINTS_KEY = "__all__"


async def _async_fetch_accounts(hass: HomeAssistant, data: dict[str, str]) -> list[dict]:
    def _fetch() -> list[dict]:
        scraper = PgeScraper(data[CONF_USERNAME], data[CONF_PASSWORD])
        return scraper.get_billing_accounts()

    return await hass.async_add_executor_job(_fetch)


class PgeEbokConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for PGE Sensor."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._accounts: list[dict] = []
        self._available: list[dict] = []

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME]
            try:
                accounts = await _async_fetch_accounts(self.hass, user_input)
            except PgeAuthError:
                errors["base"] = "invalid_auth"
            except PgeScraperError:
                # Includes PgeBlockedError (PGE WAF/bot defense): not a password problem.
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                self._username = username
                self._password = user_input[CONF_PASSWORD]
                self._accounts = accounts

                # Compare account numbers as strings without leading zeros:
                # "01234567" and "001234567" are the same contract.
                existing_ids = {
                    number_key(entry.data.get(CONF_ACCOUNT_ID))
                    for entry in self._async_current_entries()
                    if entry.data[CONF_USERNAME].strip().lower() == username.strip().lower()
                }
                self._available = [acc for acc in accounts if number_key(acc.get("id")) not in existing_ids]

                if not accounts:
                    # No billing accounts reported at all; fall back to a single
                    # entry without an account id (legacy single-account behavior).
                    return await self._async_finish({})
                if not self._available:
                    return self.async_abort(reason="already_configured")
                if len(self._available) == 1:
                    return await self._async_finish(self._available[0])
                return await self.async_step_select_point()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )

        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)

    async def async_step_select_point(self, user_input: dict[str, str] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        options = {ALL_POINTS_KEY: f"Wszystkie punkty poboru ({len(self._available)})"}
        options.update({str(acc["id"]): describe_billing_account(acc) for acc in self._available})

        if user_input is not None:
            choice = user_input["point"]
            if choice == ALL_POINTS_KEY:
                first, *rest = self._available
                for acc in rest:
                    self.hass.async_create_task(
                        self.hass.config_entries.flow.async_init(
                            DOMAIN,
                            context={"source": config_entries.SOURCE_IMPORT},
                            data={
                                CONF_USERNAME: self._username,
                                CONF_PASSWORD: self._password,
                                CONF_ACCOUNT_ID: acc["id"],
                                "label": describe_billing_account(acc),
                            },
                        )
                    )
                return await self._async_finish(first)

            chosen = next((acc for acc in self._available if str(acc["id"]) == choice), None)
            if chosen is None:
                errors["base"] = "unknown"
            else:
                return await self._async_finish(chosen)

        data_schema = vol.Schema({vol.Required("point", default=ALL_POINTS_KEY): vol.In(options)})
        return self.async_show_form(step_id="select_point", data_schema=data_schema, errors=errors)

    async def async_step_import(self, import_data: dict[str, Any]) -> FlowResult:
        """Create an entry queued by the "all points" selection, without further prompts."""
        username = import_data[CONF_USERNAME]
        account_id = import_data[CONF_ACCOUNT_ID]

        await self.async_set_unique_id(f"{username.lower()}_{account_id}")
        self._abort_if_unique_id_configured()
        for entry in self._async_current_entries():
            if entry.data[CONF_USERNAME].strip().lower() == username.strip().lower() and number_key(
                entry.data.get(CONF_ACCOUNT_ID)
            ) == number_key(account_id):
                return self.async_abort(reason="already_configured")

        return self.async_create_entry(
            title=import_data.get("label") or f"{username} ({account_id})",
            data={
                CONF_USERNAME: username,
                CONF_PASSWORD: import_data[CONF_PASSWORD],
                CONF_ACCOUNT_ID: account_id,
            },
        )

    async def _async_finish(self, account: dict) -> FlowResult:
        account_id = account.get("id")
        unique_id = f"{self._username.lower()}_{account_id}" if account_id is not None else self._username.lower()
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        data: dict[str, Any] = {CONF_USERNAME: self._username, CONF_PASSWORD: self._password}
        if account_id is not None:
            data[CONF_ACCOUNT_ID] = account_id

        title = describe_billing_account(account) if len(self._accounts) > 1 else self._username
        return self.async_create_entry(title=title, data=data)
