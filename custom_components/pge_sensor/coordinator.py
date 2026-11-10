"""DataUpdateCoordinator for the PGE Sensor integration."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CONTRACT_ENDED,
    BalanceInfo,
    PgeAuthError,
    PgeBlockedError,
    PgeScraper,
    PgeScraperError,
    normalize_number,
)

SCAN_INTERVAL = timedelta(hours=8)
# Back-off after failures. A blocked client must not hammer PGE: every retry
# from a flagged address only prolongs the block.
RETRY_INTERVALS = (timedelta(minutes=30), timedelta(hours=1), timedelta(hours=2), timedelta(hours=4))
BLOCKED_INTERVALS = (timedelta(hours=1), timedelta(hours=3), timedelta(hours=6), timedelta(hours=12))
AUTH_INTERVAL = timedelta(hours=12)
_LOGGER = logging.getLogger(__name__)


def anonymous_label(username: str, account_id: object | None) -> str:
    """Stable, non-reversible label for logs (no e-mail, no account number)."""
    digest = hashlib.sha256(f"{username.lower()}|{normalize_number(account_id)}".encode()).hexdigest()
    return digest[:8]


class PgeEbokCoordinator(DataUpdateCoordinator[BalanceInfo]):
    """Fetches data of one billing account (contract)."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: PgeScraper,
        username: str,
        account_id: object | None = None,
        ppe: str | None = None,
        contract_ended: bool = False,
    ) -> None:
        self._api = api
        self._username = username
        self._account_id = normalize_number(account_id) if account_id is not None else None
        self._ppe = ppe
        self._contract_ended = contract_ended
        self.last_successful_sync: datetime | None = None
        self.last_error: str | None = None
        self.last_support_id: str | None = None
        self.consecutive_failures = 0
        super().__init__(
            hass,
            _LOGGER,
            # The coordinator name is printed by HA in every error line.
            name=f"PGE Sensor [{anonymous_label(username, account_id)}]",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> BalanceInfo:
        try:
            data = await self.hass.async_add_executor_job(
                self._api.get_balance_details, self._account_id, self._ppe
            )
        except PgeBlockedError as err:
            self._failed(err, BLOCKED_INTERVALS)
            self.last_support_id = err.support_id
            raise UpdateFailed(
                "PGE WAF/bot defense blocked the request; backing off "
                f"{self.update_interval}. Check https://ebok.gkpge.pl in a browser from the same network"
            ) from err
        except PgeAuthError as err:
            self._failed(err, (AUTH_INTERVAL,))
            raise UpdateFailed("PGE rejected the credentials (check password in eBOK)") from err
        except PgeScraperError as err:
            self._failed(err, RETRY_INTERVALS)
            raise UpdateFailed(f"{err} (retry in {self.update_interval})") from err
        except Exception as err:  # pragma: no cover - defensive guard
            self._failed(err, RETRY_INTERVALS)
            raise UpdateFailed(f"Unexpected coordinator error: {type(err).__name__}") from err

        if self._contract_ended:
            data.contract_status = CONTRACT_ENDED
            data.contract_status_source = "user_option"
            data.meter_owner = False
        self.consecutive_failures = 0
        self.last_error = None
        self.last_successful_sync = dt_util.utcnow()
        self._set_interval(SCAN_INTERVAL)
        return data

    def _failed(self, err: Exception, schedule: tuple[timedelta, ...]) -> None:
        self.consecutive_failures += 1
        self.last_error = type(err).__name__
        self._set_interval(schedule[min(self.consecutive_failures - 1, len(schedule) - 1)])

    @property
    def username(self) -> str:
        return self._username

    @property
    def account_id(self) -> str | None:
        return self._account_id

    def _set_interval(self, interval: timedelta) -> None:
        if self.update_interval != interval:
            self.update_interval = interval
