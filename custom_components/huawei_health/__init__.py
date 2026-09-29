"""The Huawei Health integration.

Polls the Health app's own cloud for one (or several) HUAWEI ID accounts. Each config
entry carries its own rotating token pair, so accounts are fully independent.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError

from .client import HuaweiHealthClient, Tokens
from .const import (
    APP_VERSION,
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_CODE,
    CONF_EXPIRES_AT,
    CONF_HOST,
    CONF_REFRESH_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_SESSION_HOST,
    CONF_SITE_ID,
    CONF_TOKEN_TYPE,
    CONF_UID,
    PLATFORMS,
    TOKEN_TYPE,
)
from .coordinator import HuaweiHealthCoordinator, HuaweiHealthData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Build the client, prove the credentials work, then hand the platforms over."""
    if not entry.data.get(CONF_REFRESH_TOKEN):
        raise ConfigEntryError("Config entry carries no refresh token; re-add the account")

    data = _build(hass, entry)
    await data.coordinator.async_config_entry_first_refresh()

    entry.runtime_data = data
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


def _build(hass: HomeAssistant, entry: ConfigEntry) -> HuaweiHealthData:
    tokens = Tokens(
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        uid=entry.data.get(CONF_UID),
        site_id=entry.data.get(CONF_SITE_ID),
        expires_at=float(entry.data.get(CONF_EXPIRES_AT) or 0),
        refresh_expires_at=float(entry.data.get(CONF_REFRESH_EXPIRES_AT) or 0),
        session_host=entry.data.get(CONF_SESSION_HOST),
        on_change=lambda current: _schedule_save(hass, entry, current),
    )
    client = HuaweiHealthClient(
        tokens,
        host=entry.data.get(CONF_HOST),
        device_code=entry.data.get(CONF_DEVICE_CODE),
        version=APP_VERSION,
        token_type=int(entry.data.get(CONF_TOKEN_TYPE) or TOKEN_TYPE),
    )
    coordinator = HuaweiHealthCoordinator(hass, entry, client, tokens)
    return HuaweiHealthData(entry=entry, client=client, tokens=tokens,
                            coordinator=coordinator, options=dict(entry.options))


def _schedule_save(hass: HomeAssistant, entry: ConfigEntry, tokens: Tokens) -> None:
    """Persist a rotated pair from whatever thread rotated it.

    Rotation invalidates the previous access token server-side, so losing this write means
    losing the session until the account logs in again - hence threadsafe scheduling.
    """
    hass.loop.call_soon_threadsafe(_save_tokens, hass, entry, tokens)


@callback
def _save_tokens(hass: HomeAssistant, entry: ConfigEntry, tokens: Tokens) -> None:
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_ACCESS_TOKEN: tokens.access_token,
            CONF_REFRESH_TOKEN: tokens.refresh_token,
            CONF_UID: tokens.uid,
            CONF_SITE_ID: tokens.site_id,
            CONF_EXPIRES_AT: tokens.expires_at,
            CONF_REFRESH_EXPIRES_AT: tokens.refresh_expires_at,
            CONF_SESSION_HOST: tokens.session_host,
        },
    )
    _LOGGER.debug("Stored the rotated Huawei Health token for %s", entry.title)


@callback
def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on an options change only - token saves land on the same listener."""
    data: HuaweiHealthData | None = getattr(entry, "runtime_data", None)
    if data is None or dict(entry.options) == data.options:
        return
    hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    entry.runtime_data = None
    return True
