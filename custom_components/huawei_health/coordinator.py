"""Coordinator for the Huawei Health integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .client import (
    HuaweiAuthError,
    HuaweiConnectionError,
    HuaweiError,
    HuaweiHealthClient,
    Tokens,
    daily_totals,
)
from .const import (
    CONF_ENABLE_HEALTH,
    CONF_ENABLE_WORKOUTS,
    CONF_HISTORY_DAYS,
    CONF_UPDATE_INTERVAL,
    CONF_WORKOUT_DAYS,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_WORKOUT_DAYS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class HuaweiHealthData:
    """Everything an entity needs, hanging off entry.runtime_data."""

    entry: ConfigEntry
    client: HuaweiHealthClient
    tokens: Tokens
    coordinator: HuaweiHealthCoordinator
    options: dict

    def day(self, metric: str, lookback_days: int = 0) -> tuple[str, Any] | None:
        """Newest value of a per-day metric, with the date it belongs to.

        The cloud stamps a night with the day it ended on and fills a day lazily as the
        band syncs, so an empty today is normal for a few hours and the previous day is
        the honest reading rather than "unavailable".
        """
        days = (self.coordinator.data or {}).get("days") or {}
        offset = date.today()
        for _ in range(lookback_days + 1):
            row = days.get(offset.isoformat())
            if row and row.get(metric) is not None:
                return offset.isoformat(), row[metric]
            offset -= timedelta(days=1)
        return None

    @property
    def latest_session(self) -> dict | None:
        return (self.coordinator.data or {}).get("latest_session")

    @property
    def sessions(self) -> list[dict]:
        return (self.coordinator.data or {}).get("sessions") or []


class HuaweiHealthCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the account's cloud, in a worker thread, with a self-rotating token."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry,
                 client: HuaweiHealthClient, tokens: Tokens) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=int(entry.options.get(CONF_UPDATE_INTERVAL)
                            or DEFAULT_UPDATE_INTERVAL.total_seconds() // 60)),
        )
        self.client = client
        self.tokens = tokens

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.hass.async_add_executor_job(self._gather)
        except HuaweiAuthError as err:
            # Only a fresh browser login clears this, so ask HA for the reauth card.
            raise ConfigEntryAuthFailed(str(err)) from err
        except HuaweiConnectionError as err:
            raise UpdateFailed(f"cannot reach the Huawei Health cloud: {err}") from err
        except HuaweiError as err:
            raise UpdateFailed(str(err)) from err

    def _gather(self) -> dict[str, Any]:
        entry = self.config_entry
        history = int(entry.options.get(CONF_HISTORY_DAYS) or DEFAULT_HISTORY_DAYS)
        days: dict[str, dict] = {}
        failures: list[str] = []

        def run(name: str, fetch, merge) -> None:
            try:
                merge(fetch())
            except HuaweiAuthError:
                raise
            except HuaweiError as err:
                # A family the account's devices never fill must not hide the rest.
                _LOGGER.debug("Huawei Health %s read failed: %s", name, err)
                failures.append(f"{name}: {err}")

        def merge_daily(rows: list[dict]) -> None:
            for day, row in daily_totals(rows).items():
                days.setdefault(day, {}).update(row)

        def merge_health(rows: list[dict]) -> None:
            for row in rows:
                days.setdefault(row["date"], {}).update(row)

        run("activity", lambda: self.client.daily_summary(history), merge_daily)
        if entry.options.get(CONF_ENABLE_HEALTH, True):
            run("health", lambda: self.client.health_summary(history), merge_health)

        workouts: list[dict] = []
        window_days = int(entry.options.get(CONF_WORKOUT_DAYS) or DEFAULT_WORKOUT_DAYS)
        if entry.options.get(CONF_ENABLE_WORKOUTS, True):
            def fetch_sessions() -> list[dict]:
                return self.client.sessions(window_days)

            run("workouts", fetch_sessions, lambda rows: workouts.extend(rows))

        if not days and failures:
            raise UpdateFailed("; ".join(failures))
        workouts.sort(key=lambda session: session["start"])
        return {
            "days": days,
            "sessions": workouts,
            "latest_session": workouts[-1] if workouts else None,
            "window_days": window_days,
        }
