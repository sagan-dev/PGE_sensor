"""Home Assistant integration for PGE Sensor."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .api import PgeScraper, number_key
from .const import CONF_ACCOUNT_ID, CONF_PASSWORD, CONF_PPE, CONF_USERNAME, DEFAULT_TIMEOUT, DOMAIN
from .coordinator import PgeEbokCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)
_CLIENTS = "clients"


async def async_setup(_hass: HomeAssistant, _config: ConfigType) -> bool:
    """Set up the integration via YAML (not supported)."""
    return True


def _client_for(hass: HomeAssistant, entry: ConfigEntry) -> PgeScraper:
    """One API client (one login, one token) per PGE account login."""
    clients: dict = hass.data[DOMAIN].setdefault(_CLIENTS, {})
    key = entry.data[CONF_USERNAME].strip().lower()
    holder = clients.get(key)
    if holder is None or holder["password"] != entry.data[CONF_PASSWORD]:
        holder = {
            "client": PgeScraper(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD], timeout=DEFAULT_TIMEOUT),
            "password": entry.data[CONF_PASSWORD],
            "entries": set(),
        }
        clients[key] = holder
    holder["entries"].add(entry.entry_id)
    return holder["client"]


def _release_client(hass: HomeAssistant, entry: ConfigEntry) -> None:
    clients: dict = hass.data[DOMAIN].get(_CLIENTS, {})
    key = entry.data[CONF_USERNAME].strip().lower()
    holder = clients.get(key)
    if holder:
        holder["entries"].discard(entry.entry_id)
        if not holder["entries"]:
            clients.pop(key, None)


def _warn_duplicates(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Two entries for the same login and the same contract duplicate history."""
    own = (entry.data[CONF_USERNAME].strip().lower(), number_key(entry.data.get(CONF_ACCOUNT_ID)))
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry.entry_id:
            continue
        if (other.data[CONF_USERNAME].strip().lower(), number_key(other.data.get(CONF_ACCOUNT_ID))) == own:
            _LOGGER.warning(
                "Two PGE Sensor entries point to the same contract; remove one of them "
                "(see diagnostics of the entries, field 'duplicate_of')"
            )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    _warn_duplicates(hass, entry)
    coordinator = PgeEbokCoordinator(
        hass,
        _client_for(hass, entry),
        entry.data[CONF_USERNAME],
        entry.data.get(CONF_ACCOUNT_ID),
        entry.data.get(CONF_PPE),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _release_client(hass, entry)
    return unload_ok
