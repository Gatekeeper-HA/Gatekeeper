"""Doorbell presses from a Reolink doorbell, via reolink-aio.

reolink-aio logs in over HTTPS and subscribes to the camera's Baichuan push
events (TCP 9000), the same mechanism Home Assistant's Reolink integration
uses, so this works without Home Assistant. A press is the doorbell's
"visitor" state turning on.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress

log = logging.getLogger(__name__)

CALLBACK_ID = "gatekeeper_doorbell_button"


def _reolink_host(host: str, username: str, password: str):
    from reolink_aio.api import Host

    return Host(host, username, password, port=443, use_https=True)


class DoorbellButton:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        on_press: Callable[[], None],
        channel: int = 0,
        renew_interval: float = 60.0,
        host_factory: Callable[[str, str, str], object] = _reolink_host,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.channel = channel
        self.renew_interval = renew_interval
        self._on_press = on_press
        self._host_factory = host_factory
        self._pressed = False
        self.disabled = False

    def check(self, api) -> None:
        """Called on every push event: report a press on the visitor state's rising edge."""
        pressed = bool(api.visitor_detected(self.channel))
        if pressed and not self._pressed:
            log.info("doorbell button pressed")
            try:
                self._on_press()
            except Exception:
                log.exception("doorbell press handler failed")
        self._pressed = pressed

    async def run(self) -> None:
        api = self._host_factory(self.host, self.username, self.password)
        try:
            await api.get_host_data()
            if not api.is_doorbell(self.channel):
                log.error("%s (%s) is not a doorbell; presses disabled", api.model, self.host)
                self.disabled = True
                return
            self._pressed = bool(api.visitor_detected(self.channel))
            api.baichuan.register_callback(CALLBACK_ID, lambda: self.check(api))
            await api.baichuan.subscribe_events()
            log.info("listening for button presses on %s (firmware %s)", api.model, api.sw_version)
            while True:
                await asyncio.sleep(self.renew_interval)
                await api.baichuan.check_subscribe_events()
        finally:
            with suppress(Exception):
                api.baichuan.unregister_callback(CALLBACK_ID)
                await api.baichuan.unsubscribe_events()
            with suppress(Exception):
                await api.logout()

    def run_forever(self, max_backoff: float = 300.0) -> None:
        backoff = 5.0
        while True:
            started = time.monotonic()
            try:
                asyncio.run(self.run())
            except Exception as e:
                log.error("doorbell button listener stopped: %s", e)
            if self.disabled:
                return
            if time.monotonic() - started > 600:
                backoff = 5.0  # it was working; reconnect quickly
            log.info("reconnecting to the doorbell in %.0f s", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
