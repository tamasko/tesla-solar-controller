"""Authenticated read-only activity viewer; never interacts with Tesla."""

import logging
from pathlib import Path

from homeassistant.components import frontend, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from .activity_log import ActivityLog
from .const import DOMAIN

PANEL_PATH = "tesla-solar-controller-log"
STATIC_URL = "/tesla_solar_controller_static/log-panel.js"
_LOGGER = logging.getLogger(__name__)


async def async_setup_activity_log(hass):
    """Register a private log reader and a hidden frontend panel once per HA run."""
    if DOMAIN in hass.data:
        return
    try:
        writer = await hass.async_add_executor_job(
            ActivityLog, hass.config.path("tesla_solar_controller.log")
        )
    except OSError:
        _LOGGER.exception("Activity file unavailable; controller will continue")
        writer = None
    hass.data[DOMAIN] = {"activity_log": writer}

    async def shutdown(event):
        if writer:
            await hass.async_add_executor_job(writer.close)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown)
    websocket_api.async_register_command(hass, websocket_activity_log)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(Path(__file__).with_name("log-panel.js")), False)]
    )
    frontend.async_register_built_in_panel(
        hass,
        component_name="custom",
        frontend_url_path=PANEL_PATH,
        require_admin=True,
        show_in_sidebar=False,
        config={
            "_panel_custom": {
                "name": PANEL_PATH,
                "module_url": STATIC_URL,
                "embed_iframe": False,
                "trust_external": False,
            }
        },
    )


@websocket_api.websocket_command({"type": f"{DOMAIN}/activity_log"})
@websocket_api.async_response
async def websocket_activity_log(hass, connection, msg):
    """Read only this integration's fixed file, and only for authenticated admins."""
    if connection.user is None or not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Administrator access required")
        return
    writer = hass.data[DOMAIN]["activity_log"]
    if writer is None:
        connection.send_error(
            msg["id"], "log_unavailable", "Activity file is unavailable; check HA logs"
        )
        return
    try:
        content = await hass.async_add_executor_job(writer.read)
    except OSError:
        _LOGGER.exception("Cannot read controller activity file")
        connection.send_error(msg["id"], "log_unavailable", "Cannot read activity file")
        return
    connection.send_result(
        msg["id"], {"filename": "tesla_solar_controller.log", "content": content}
    )
