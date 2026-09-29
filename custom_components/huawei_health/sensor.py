"""Sensors for the Huawei Health integration."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import SPORT_TYPES
from .coordinator import HuaweiHealthData
from .entity import HuaweiHealthEntity

UNIT_STEPS: str = "steps"
UNIT_BPM: str = "bpm"
UNIT_SCORE: str = "score"


@dataclass(kw_only=True)
class HuaweiSensorDescription(SensorEntityDescription):
    """A sensor plus how to pull its number out of the coordinator snapshot."""

    value: Callable[[HuaweiHealthData], Any] = lambda data: None
    attributes: Callable[[HuaweiHealthData], dict[str, Any]] = lambda data: {}


def day_value(metric: str, lookback: int = 0, scale: float = 1.0
              ) -> Callable[[HuaweiHealthData], Any]:
    """Read one per-day metric, falling back to the previous days if needed."""

    def get(data: HuaweiHealthData) -> Any:
        found = data.day(metric, lookback)
        return None if found is None else found[1] * scale

    return get


def day_date(metric: str, lookback: int = 0) -> Callable[[HuaweiHealthData], dict]:
    def attributes(data: HuaweiHealthData) -> dict[str, Any]:
        found = data.day(metric, lookback)
        return {} if found is None else {"date": found[0]}

    return attributes


def session_value(key: str, scale: float = 1.0
                  ) -> Callable[[HuaweiHealthData], Any]:
    def get(data: HuaweiHealthData) -> Any:
        session = data.latest_session
        value = None if session is None else session.get(key)
        return None if value is None else value * scale

    return get


def session_attributes(data: HuaweiHealthData) -> dict[str, Any]:
    session = data.latest_session
    if not session:
        return {}
    return {
        "start": session["start"].strftime("%Y-%m-%d %H:%M"),
        "end": session["end"].strftime("%Y-%m-%d %H:%M"),
        "duration_min": session["duration_min"],
        "distance_m": session["distance_m"],
        "calories_kcal": session["kcal"],
        "segments": session["segments"],
        "sport_type": session["sport_type"],
        "device_code": session["device_code"],
    }


def activity_name(data: HuaweiHealthData) -> str | None:
    session = data.latest_session
    if not session:
        return None
    # A state outside the declared options makes HA log a warning, so unknown ids from a
    # newer app version collapse to one value.
    return SPORT_TYPES.get(session["sport_type"], "unknown")


def _days_left(data: HuaweiHealthData) -> float | None:
    deadline = data.tokens.refresh_expires_at
    return None if not deadline else round((deadline - time.time()) / 86400, 1)


def _token_dates(data: HuaweiHealthData) -> dict[str, Any]:
    def stamp(seconds: float) -> str:
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")

    return {
        "access_token_expires": stamp(data.tokens.expires_at),
        "refresh_token_expires": stamp(data.tokens.refresh_expires_at),
    }


DESCRIPTIONS: tuple[HuaweiSensorDescription, ...] = (
    # ── the day's totals, matching the Health app's activity rings ─────────
    HuaweiSensorDescription(
        key="steps", translation_key="steps", native_unit_of_measurement=UNIT_STEPS,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:walk",
        value=day_value("steps"), attributes=day_date("steps")),
    HuaweiSensorDescription(
        key="distance", translation_key="distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=2, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-distance",
        value=day_value("distance_m", scale=1 / 1000), attributes=day_date("distance_m")),
    HuaweiSensorDescription(
        key="calories", translation_key="calories",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_CALORIE,
        suggested_display_precision=0, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fire",
        value=day_value("kcal"), attributes=day_date("kcal")),
    HuaweiSensorDescription(
        key="activity_duration", translation_key="activity_duration",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:timer-outline",
        value=day_value("duration_min"), attributes=day_date("duration_min")),
    HuaweiSensorDescription(
        key="walking_duration", translation_key="walking_duration",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:shoe-print",
        value=day_value("walk_min"), attributes=day_date("walk_min")),
    HuaweiSensorDescription(
        key="active_hours", translation_key="active_hours",
        native_unit_of_measurement=UnitOfTime.HOURS, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock-outline",
        value=day_value("active_hours"), attributes=day_date("active_hours")),
    HuaweiSensorDescription(
        key="moderate_high_intensity_minutes",
        translation_key="moderate_high_intensity_minutes",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:run-fast",
        value=day_value("moderate_high_intensity_minutes"),
        attributes=day_date("moderate_high_intensity_minutes")),
    HuaweiSensorDescription(
        key="step_goal", translation_key="step_goal", native_unit_of_measurement=UNIT_STEPS,
        icon="mdi:target-variant", entity_registry_enabled_default=False,
        value=day_value("step_goal"), attributes=day_date("step_goal")),
    # ── heart rate ─────────────────────────────────────────────────────────
    HuaweiSensorDescription(
        key="resting_heart_rate", translation_key="resting_heart_rate",
        native_unit_of_measurement=UNIT_BPM, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart",
        value=day_value("resting_heart_rate"),
        attributes=day_date("resting_heart_rate")),
    HuaweiSensorDescription(
        key="heart_rate", translation_key="heart_rate",
        native_unit_of_measurement=UNIT_BPM, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-pulse",
        value=day_value("heart_rate"), attributes=day_date("heart_rate")),
    HuaweiSensorDescription(
        key="max_heart_rate", translation_key="max_heart_rate",
        native_unit_of_measurement=UNIT_BPM, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-plus", entity_registry_enabled_default=False,
        value=day_value("max_heart_rate"), attributes=day_date("max_heart_rate")),
    HuaweiSensorDescription(
        key="min_heart_rate", translation_key="min_heart_rate",
        native_unit_of_measurement=UNIT_BPM, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-minus", entity_registry_enabled_default=False,
        value=day_value("min_heart_rate"), attributes=day_date("min_heart_rate")),
    # ── sleep: the night that just ended is stamped with the day it woke up in ──
    HuaweiSensorDescription(
        key="sleep_duration", translation_key="sleep_duration",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:sleep",
        value=day_value("sleep_duration", lookback=1),
        attributes=day_date("sleep_duration", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_deep", translation_key="sleep_deep",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:weather-night",
        value=day_value("sleep_deep", lookback=1),
        attributes=day_date("sleep_deep", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_rem", translation_key="sleep_rem",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:brain",
        value=day_value("sleep_rem", lookback=1),
        attributes=day_date("sleep_rem", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_light", translation_key="sleep_light",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:weather-night",
        entity_registry_enabled_default=False,
        value=day_value("sleep_light", lookback=1),
        attributes=day_date("sleep_light", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_awake", translation_key="sleep_awake",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:eye",
        entity_registry_enabled_default=False,
        value=day_value("sleep_awake", lookback=1),
        attributes=day_date("sleep_awake", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_score", translation_key="sleep_score",
        native_unit_of_measurement=UNIT_SCORE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:star-four-points-circle",
        value=day_value("sleep_score", lookback=1),
        attributes=day_date("sleep_score", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_efficiency", translation_key="sleep_efficiency",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:progress-clock", entity_registry_enabled_default=False,
        value=day_value("sleep_efficiency", lookback=1),
        attributes=day_date("sleep_efficiency", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_hrv", translation_key="sleep_hrv",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:waveform",
        value=day_value("sleep_hrv", lookback=1),
        attributes=day_date("sleep_hrv", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_spo2", translation_key="sleep_spo2",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:oximeter",
        value=day_value("sleep_spo2", lookback=1),
        attributes=day_date("sleep_spo2", lookback=1)),
    # ── stress ─────────────────────────────────────────────────────────────
    HuaweiSensorDescription(
        key="stress", translation_key="stress", native_unit_of_measurement=UNIT_SCORE,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:gauge",
        value=day_value("stress_average"), attributes=day_date("stress_average")),
    HuaweiSensorDescription(
        key="stress_last", translation_key="stress_last",
        native_unit_of_measurement=UNIT_SCORE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gauge", entity_registry_enabled_default=False,
        value=day_value("stress_last"), attributes=day_date("stress_last")),
    # ── workouts, rebuilt from the per-minute segments ─────────────────────
    HuaweiSensorDescription(
        key="last_activity", translation_key="last_activity",
        device_class=SensorDeviceClass.ENUM,
        options=list(SPORT_TYPES.values()),
        icon="mdi:sport", value=activity_name, attributes=session_attributes),
    HuaweiSensorDescription(
        key="last_activity_duration", translation_key="last_activity_duration",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:timer",
        value=session_value("duration_min"), attributes=session_attributes),
    HuaweiSensorDescription(
        key="last_activity_distance", translation_key="last_activity_distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=2, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-distance",
        value=session_value("distance_m", scale=1 / 1000),
        attributes=session_attributes),
    HuaweiSensorDescription(
        key="last_activity_calories", translation_key="last_activity_calories",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_CALORIE,
        suggested_display_precision=0, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fire", value=session_value("kcal"), attributes=session_attributes),
    HuaweiSensorDescription(
        key="workouts", translation_key="workouts", native_unit_of_measurement="workouts",
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:trophy-outline",
        value=lambda data: len(data.sessions),
        attributes=lambda data: {
            "window_days": (data.coordinator.data or {}).get("window_days")}),
    # ── session health: the one number that says when a login is due again ──
    HuaweiSensorDescription(
        key="relogin_in", translation_key="relogin_in",
        native_unit_of_measurement=UnitOfTime.DAYS, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:account-clock", suggested_display_precision=0,
        value=_days_left, attributes=_token_dates),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    data: HuaweiHealthData = entry.runtime_data
    async_add_entities(HuaweiHealthSensor(description, data)
                       for description in DESCRIPTIONS)


class HuaweiHealthSensor(HuaweiHealthEntity, SensorEntity):
    entity_description: HuaweiSensorDescription

    def __init__(self, description: HuaweiSensorDescription,
                 data: HuaweiHealthData) -> None:
        super().__init__(data)
        self.entity_description = description
        self._attr_unique_id = f"{data.entry.entry_id}_{description.key}"
        # translation_key comes from the description, so names live in translations/

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self._data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attributes(self._data)
