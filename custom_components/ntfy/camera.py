"""Camera platform that receives images through HTTP POST."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
import logging
import httpx

from homeassistant.helpers.httpx_client import get_async_client

import voluptuous as vol

from .const import (
  DOMAIN, 
  CONF_TOPIC,
  GET_IMAGE_TIMEOUT,
)

from homeassistant.components.camera import PLATFORM_SCHEMA, STATE_IDLE, Camera
from homeassistant.const import (
    CONF_NAME,
    CONF_TIMEOUT,
    CONF_HOST,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
import homeassistant.util.dt as dt_util

from .ws_client import NtfyWsClient

_LOGGER = logging.getLogger(__name__)

CONF_BUFFER_SIZE = "buffer"
CONF_IMAGE_FIELD = "field"

DEFAULT_NAME = "ntfy Camera"

ATTR_FILENAME = "filename"
ATTR_LAST_TRIP = "last_trip"

NTFY_CAMERA_DATA = "ntfy_camera"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_TOPIC): cv.string,
        vol.Optional(CONF_HOST): cv.string,
        vol.Optional(CONF_TOKEN): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_BUFFER_SIZE, default=1): cv.positive_int,
        vol.Optional(CONF_TIMEOUT, default=timedelta(seconds=5)): vol.All(
            cv.time_period, cv.positive_timedelta
        ),
        vol.Optional(CONF_IMAGE_FIELD, default="url"): cv.string,
        vol.Optional(CONF_VERIFY_SSL, default=False): cv.boolean,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Push Camera platform."""
    if NTFY_CAMERA_DATA not in hass.data:
        hass.data[NTFY_CAMERA_DATA] = {}

    host = config.get(CONF_HOST) or 'ntfy.sh'
    token = config.get(CONF_TOKEN) or ''
    topic = config.get(CONF_TOPIC)

    client = NtfyWsClient(host, topic)

    cameras = [
        NtfyCamera(
            hass,
            host,
            topic,
            config[CONF_NAME],
            config[CONF_BUFFER_SIZE],
            config[CONF_TIMEOUT],
            config[CONF_IMAGE_FIELD],
            config[CONF_VERIFY_SSL],
            client,
        )
    ]

    async_add_entities(cameras)


class NtfyCamera(Camera):
    """The representation of a Push camera."""

    def __init__(self, hass, host, topic, name, buffer_size, timeout, image_field, verify_ssl, client):
        """Initialize push camera component."""
        super().__init__()
        self._host = host
        self._topic = topic
        self._name = name
        self._last_trip = None
        self._expired_listener = None
        self._timeout = timeout
        self.queue = deque([], buffer_size)
        self._current_image = None
        self._image_field = image_field
        self._verify_ssl = verify_ssl

        self._last_host = None
        self._last_image = None
        self._last_update = datetime.min
        self._update_lock = asyncio.Lock()

        self._client = client

        def callback(key, data):
            camera = hass.data[NTFY_CAMERA_DATA][key]
            if "attachment" not in data or "type" not in data["attachment"] or not data["attachment"]["type"].startswith("image/"):
                _LOGGER.debug("Not an image")
                return
            if camera.image_field not in data["attachment"]:
                _LOGGER.warning("topic attachment without image field <%s>", camera.image_field)
                return

            return asyncio.run_coroutine_threadsafe(
                camera.update_image(data["attachment"][camera.image_field]), hass.loop
            ).result()
        
        self._client.callback = callback

    async def async_added_to_hass(self) -> None:
        """Call when entity is added to hass."""
        self.hass.data[NTFY_CAMERA_DATA][self._host + self._topic] = self

    @property
    def image_field(self):
        """HTTP field containing the image file."""
        return self._image_field

    async def update_image(self, image_host):
        """Update the camera image."""
        if self.state == STATE_IDLE:
            self._attr_is_recording = True
            self._last_trip = dt_util.utcnow()
            self.queue.clear()

        async with self._update_lock:
            if (
                self._last_image is not None
                and image_host == self._last_host
                and self._last_update + timedelta(0, self._attr_frame_interval)
                > datetime.now()
            ):
                return

            try:
                update_time = datetime.now()
                async_client = get_async_client(self.hass, verify_ssl=self._verify_ssl)
                response = await async_client.get(
                    image_host,
                    follow_redirects=True,
                    timeout=GET_IMAGE_TIMEOUT,
                )
                response.raise_for_status()
                self._last_image = response.content
                self._last_update = update_time

            except httpx.TimeoutException:
                _LOGGER.error("Timeout getting camera image from %s", self._name)
                return
            except (httpx.RequestError, httpx.HTTPStatusError) as err:
                _LOGGER.error(
                    "Error getting new camera image from %s: %s", self._name, err
                )
                return

            self._last_host = image_host
            self.queue.appendleft(self._last_image)

        @callback
        def reset_state(now):
            """Set state to idle after no new images for a period of time."""
            self._attr_is_recording = False
            self._expired_listener = None
            _LOGGER.debug("Reset state")
            self.async_write_ha_state()

        if self._expired_listener:
            self._expired_listener()

        self._expired_listener = async_track_point_in_utc_time(
            self.hass, reset_state, dt_util.utcnow() + self._timeout
        )

        self.async_write_ha_state()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image response."""
        if self.queue:
            if self.state == STATE_IDLE:
                self.queue.rotate(1)
            self._current_image = self.queue[0]

        return self._current_image

    @property
    def name(self):
        """Return the name of this camera."""
        return self._name

    @property
    def motion_detection_enabled(self):
        """Camera Motion Detection Status."""
        return False

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            name: value
            for name, value in (
                (ATTR_LAST_TRIP, self._last_trip),
                (ATTR_FILENAME, self._last_host),
            )
            if value is not None
        }
