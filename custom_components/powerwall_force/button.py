"""Start and stop the existing timed actions directly from the device page."""

from homeassistant.components.button import ButtonEntity

from .const import CONF_MANUAL_DURATION, DOMAIN
from .entity import PowerwallEntity
from .manual import MANUAL_POWER_KEYS, manual_value

BUTTONS = (
    ("charge", "Force charge", "mdi:battery-arrow-up"),
    ("discharge", "Force discharge", "mdi:battery-arrow-down"),
    ("stop", "Stop", "mdi:stop-circle-outline"),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([DispatchButton(entry, *button) for button in BUTTONS])


class DispatchButton(PowerwallEntity, ButtonEntity):
    # Native Controls card precedes Sensors on Home Assistant's device page.
    _attr_entity_category = None

    def __init__(self, entry, action: str, name: str, icon: str) -> None:
        super().__init__(entry, action)
        self._action = action
        self._attr_name = name
        self._attr_icon = icon

    async def async_press(self) -> None:
        data = {"config_entry_id": self._entry.entry_id}
        if self._action != "stop":
            data.update(
                {
                    "duration": {"seconds": manual_value(self._entry, CONF_MANUAL_DURATION) * 60},
                    "power_w": manual_value(self._entry, MANUAL_POWER_KEYS[self._action]),
                }
            )
        await self.hass.services.async_call(
            DOMAIN, self._action, data, blocking=True, context=self._context
        )
