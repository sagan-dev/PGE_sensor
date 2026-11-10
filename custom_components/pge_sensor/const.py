"""Constants for the PGE Sensor integration."""
from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

DOMAIN = "pge_sensor"
DEFAULT_TIMEOUT = 15
CONF_ACCOUNT_ID = "account_id"
# Optional: PPE of the entry when one contract bills several meters.
CONF_PPE = "ppe"
# Option: the user marks this contract as ended (archived) by hand.
CONF_CONTRACT_ENDED = "contract_ended"

__all__ = ["DOMAIN", "CONF_USERNAME", "CONF_PASSWORD", "DEFAULT_TIMEOUT", "CONF_ACCOUNT_ID", "CONF_PPE", "CONF_CONTRACT_ENDED"]
