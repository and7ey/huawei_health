"""Config flow for Huawei Health: a browser login and a pasted authorization code.

The app's cloud only accepts a token pair, and the pair can only be minted by a HUAWEI ID
login that HMS Core would normally perform. The login itself is guarded by Huawei's
slide captcha, which no script may defeat, so the flow deliberately stops and asks a human
to finish it in a browser and paste back the address the browser ends on. Everything
around that one manual step is automated: the URL is generated per flow, the code is parsed
out of whatever text is pasted, and the pair is stored on the entry.
"""

from __future__ import annotations

import hashlib
import logging
from functools import partial
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .client import (
    HuaweiAuthError,
    HuaweiConnectionError,
    HuaweiError,
    Tokens,
    authorization_code_from,
    authorization_url,
    exchange_authorization_code,
    query_access_token,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CODE,
    CONF_DEVICE_CODE,
    CONF_ENABLE_HEALTH,
    CONF_ENABLE_WORKOUTS,
    CONF_EXPIRES_AT,
    CONF_HISTORY_DAYS,
    CONF_REFRESH_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_SESSION_HOST,
    CONF_SITE_ID,
    CONF_UID,
    CONF_UPDATE_INTERVAL,
    CONF_WORKOUT_DAYS,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_WORKOUT_DAYS,
    DOMAIN,
    MAX_HISTORY_DAYS,
    MIN_HISTORY_DAYS,
)

_LOGGER = logging.getLogger(__name__)

DOCS_URL = "https://github.com/and7ey/huawei_health#%D0%BF%D0%BE%D0%B4%D0%BA%D0%BB%D1%8E%D1%87%D0%B5%D0%BD%D0%B8%D0%B5"

_CODE_SCHEMA = vol.Schema({vol.Required(CONF_CODE): cv.string})


def _placeholders(authorize_url: str) -> dict[str, str]:
    return {"authorize_url": authorize_url, "docs": DOCS_URL}


def _entry_data(tokens: Tokens) -> dict[str, Any]:
    return {
        CONF_UID: tokens.uid,
        CONF_SITE_ID: tokens.site_id,
        CONF_ACCESS_TOKEN: tokens.access_token,
        CONF_REFRESH_TOKEN: tokens.refresh_token,
        CONF_EXPIRES_AT: tokens.expires_at,
        CONF_REFRESH_EXPIRES_AT: tokens.refresh_expires_at,
        CONF_SESSION_HOST: tokens.session_host,
    }


class HuaweiHealthConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handles a browser-assisted login, for a new account or a reauth."""

    VERSION = 1

    def __init__(self) -> None:
        self._authorize_url: str = ""
        self._entry_id: str | None = None
        self._uid: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
            config_entry: ConfigEntry) -> HuaweiHealthOptionsFlow:
        return HuaweiHealthOptionsFlow(config_entry)

    # ── new account ──────────────────────────────────────────────────────
    async def async_step_user(
            self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """The only step that can be automated is the one that builds the URL."""
        if user_input is not None:
            return await self.async_step_code()
        self._authorize_url = authorization_url()
        return self.async_show_form(
            step_id="user", description_placeholders=_placeholders(self._authorize_url))

    async def async_step_code(
            self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Exchange the pasted address for the token pair."""
        errors: dict[str, str] = {}
        if user_input is not None:
            code = authorization_code_from(user_input[CONF_CODE])
            if not code:
                errors[CONF_CODE] = "no_code"
            else:
                try:
                    tokens = await self.hass.async_add_executor_job(
                        partial(exchange_authorization_code, code, uid=self._uid))
                except HuaweiAuthError as err:
                    # The code is single-use, so this run of the flow needs its own page.
                    _LOGGER.error("Huawei refused the authorization code: %s", err)
                    errors[CONF_CODE] = "invalid_code"
                    self._authorize_url = authorization_url()
                except HuaweiConnectionError:
                    errors[CONF_CODE] = "cannot_connect"
                except HuaweiError as err:
                    _LOGGER.error("Huawei token exchange failed: %s", err)
                    errors[CONF_CODE] = "unknown"
                else:
                    return await self._finish(tokens)
        return self.async_show_form(
            step_id="code", data_schema=_CODE_SCHEMA, errors=errors,
            description_placeholders=_placeholders(self._authorize_url))

    async def _finish(self, tokens: Tokens) -> FlowResult:
        data = _entry_data(tokens)
        if self._entry_id:
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            if entry is not None:
                return self.async_update_reload_and_abort(
                    entry, data_updates=data, unique_id=tokens.uid or entry.unique_id)
        unique_id = tokens.uid or hashlib.sha256(
            (tokens.access_token or "").encode()).hexdigest()[:16]
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured(updates=data)
        name = await self.hass.async_add_executor_job(
            partial(_profile_name, tokens.access_token, tokens.session_host))
        return self.async_create_entry(title=name or f"Huawei {unique_id[-4:]}",
                                       data=data)

    # ── reauth ───────────────────────────────────────────────────────────
    async def async_step_reauth(
            self, entry_data: dict[str, Any]) -> FlowResult:
        """The refresh token died (Huawei's 180 days are up, or the account logged out)."""
        self._entry_id = self.context.get("entry_id")
        self._uid = entry_data.get(CONF_UID)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
            self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return await self.async_step_code()
        self._authorize_url = authorization_url()
        entry = self.hass.config_entries.async_get_entry(self._entry_id or "")
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders=dict(
                _placeholders(self._authorize_url),
                account=entry.title if entry else ""))

    # ── imported entries (none: this integration has no YAML config) ─────
    async def async_step_import(self, import_data: dict[str, Any]) -> FlowResult:
        return self.async_abort(reason="unsupported_import")


def _profile_name(access_token: str | None, session_host: str | None) -> str | None:
    """The HUAWEI ID nickname, for the entry title; the title never blocks a setup."""
    if not access_token:
        return None
    try:
        info = query_access_token(access_token, session_host=session_host)
    except HuaweiError as err:
        _LOGGER.debug("Could not read the HUAWEI ID profile: %s", err)
        return None
    name = info.get("nickName") or info.get("displayName")
    return str(name) if name else None


class HuaweiHealthOptionsFlow(config_entries.OptionsFlow):
    """Per-account tuning: how often to poll and how far back to look."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
            self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            options = {
                CONF_UPDATE_INTERVAL: int(user_input[CONF_UPDATE_INTERVAL]),
                CONF_HISTORY_DAYS: int(user_input[CONF_HISTORY_DAYS]),
                CONF_WORKOUT_DAYS: int(user_input[CONF_WORKOUT_DAYS]),
                CONF_ENABLE_WORKOUTS: bool(user_input[CONF_ENABLE_WORKOUTS]),
                CONF_ENABLE_HEALTH: bool(user_input[CONF_ENABLE_HEALTH]),
            }
            device_code = user_input.get(CONF_DEVICE_CODE)
            if device_code:
                options[CONF_DEVICE_CODE] = int(device_code)
            else:
                options.pop(CONF_DEVICE_CODE, None)
            return self.async_create_entry(title="", data=options)

        current = self._entry.options
        days = vol.All(vol.Coerce(int), vol.Range(min=MIN_HISTORY_DAYS,
                                                 max=MAX_HISTORY_DAYS))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_UPDATE_INTERVAL,
                             default=current.get(CONF_UPDATE_INTERVAL)
                             or int(DEFAULT_UPDATE_INTERVAL.total_seconds() // 60)):
                    vol.All(vol.Coerce(int), vol.Range(min=5, max=1440)),
                vol.Optional(CONF_HISTORY_DAYS,
                             default=current.get(CONF_HISTORY_DAYS,
                                                 DEFAULT_HISTORY_DAYS)): days,
                vol.Optional(CONF_WORKOUT_DAYS,
                             default=current.get(CONF_WORKOUT_DAYS,
                                                 DEFAULT_WORKOUT_DAYS)): days,
                vol.Optional(CONF_ENABLE_WORKOUTS,
                             default=current.get(CONF_ENABLE_WORKOUTS, True)): bool,
                vol.Optional(CONF_ENABLE_HEALTH,
                             default=current.get(CONF_ENABLE_HEALTH, True)): bool,
                vol.Optional(CONF_DEVICE_CODE,
                             default=current.get(CONF_DEVICE_CODE) or 0): cv.positive_int,
            }))
