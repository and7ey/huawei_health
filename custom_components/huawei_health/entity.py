"""Base entity for the Huawei Health integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HuaweiHealthCoordinator, HuaweiHealthData

CLOUD_URL = "https://health.cloud.huawei.com"


class HuaweiHealthEntity(CoordinatorEntity[HuaweiHealthCoordinator]):
    """One account is one service device; every sensor of it hangs off that device."""

    # Disable entity naming to avoid area prefix in entity_id
    _attr_has_entity_name = False

    def __init__(self, data: HuaweiHealthData) -> None:
        super().__init__(data.coordinator)
        self._data = data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, data.entry.entry_id)},
            name=data.entry.title or "Huawei Health",
            manufacturer="Huawei",
            model="Health Cloud API",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=CLOUD_URL,
        )

    @property
    def available(self) -> bool:
        return super().available and self._data.coordinator.data is not None
