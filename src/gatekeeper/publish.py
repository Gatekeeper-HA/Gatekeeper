"""Publishing visits over MQTT, with Home Assistant MQTT discovery.

Topics (all retained):

- ``gatekeeper/status``: ``online``, or ``offline`` (the client's last will).
- ``gatekeeper/<camera>/state``: ``conversation`` while a visit runs, else ``idle``.
- ``gatekeeper/<camera>/visit``: the latest visit record (JSON, as in events.jsonl).
- ``gatekeeper/<camera>/package``: ``on`` while a package is at the door, else ``off``
  (with package alerts on).

Gatekeeper listens on ``gatekeeper/<camera>/quick_reply`` (quick_reply.py).

Discovery gives Home Assistant a "Last visitor" sensor (classification, or the
outcome if the visitor was never greeted, with the record as attributes), a
"Conversation" binary sensor, a button per quick reply and, with package
alerts, a "Package" binary sensor, grouped under one device per camera.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from gatekeeper.quick_reply import LABELS

log = logging.getLogger(__name__)

STATUS_TOPIC = "gatekeeper/status"
QUICK_REPLY_TOPIC = "gatekeeper/{camera}/quick_reply"


class Publisher:
    def __init__(
        self,
        client,
        *,
        camera: str,
        discovery_prefix: str,
        version: str,
        quick_replies: Sequence[str] = (),
        packages: bool = False,
    ) -> None:
        self.client = client
        self.camera = camera
        self.discovery_prefix = discovery_prefix
        self.version = version
        self.quick_replies = tuple(quick_replies)
        self.state_topic = f"gatekeeper/{camera}/state"
        self.visit_topic = f"gatekeeper/{camera}/visit"
        self.quick_reply_topic = QUICK_REPLY_TOPIC.format(camera=camera)
        self.packages = packages
        self.package_topic = f"gatekeeper/{camera}/package"

    def set_will(self) -> None:
        """Call before connecting: the broker publishes "offline" if we vanish."""
        self.client.will_set(STATUS_TOPIC, "offline", qos=1, retain=True)

    def announce(self) -> None:
        """Call on every (re)connect."""
        self._publish(STATUS_TOPIC, "online")
        for topic, config in self.discovery().items():
            self._publish(topic, json.dumps(config))
        self._publish(self.state_topic, "idle")

    def conversation(self, active: bool) -> None:
        self._publish(self.state_topic, "conversation" if active else "idle")

    def package(self, present: bool) -> None:
        self._publish(self.package_topic, "on" if present else "off")

    def visit(self, record: dict) -> None:
        self._publish(self.visit_topic, json.dumps(record, ensure_ascii=False))

    def discovery(self) -> dict[str, dict]:
        """Home Assistant MQTT discovery configs by topic (empty if disabled)."""
        if not self.discovery_prefix:
            return {}
        node = f"gatekeeper_{self.camera}"
        device = {
            "identifiers": [node],
            "name": f"Gatekeeper {self.camera.replace('_', ' ')}",
            "manufacturer": "Gatekeeper",
            "model": "AI doorbell assistant",
            "sw_version": self.version,
        }
        common = {"availability_topic": STATUS_TOPIC, "device": device}
        buttons = {
            f"{self.discovery_prefix}/button/{node}/quick_reply_{reply}/config": {
                "name": f"Say: {LABELS[reply]}",
                "unique_id": f"{node}_quick_reply_{reply}",
                "command_topic": self.quick_reply_topic,
                "payload_press": reply,
                "icon": "mdi:message-reply-text",
                **common,
            }
            for reply in self.quick_replies
        }
        if self.packages:
            buttons[f"{self.discovery_prefix}/binary_sensor/{node}/package/config"] = {
                "name": "Package",
                "unique_id": f"{node}_package",
                "state_topic": self.package_topic,
                "payload_on": "on",
                "payload_off": "off",
                "icon": "mdi:package-variant-closed",
                **common,
            }
        return {
            f"{self.discovery_prefix}/sensor/{node}/last_visitor/config": {
                "name": "Last visitor",
                "unique_id": f"{node}_last_visitor",
                "state_topic": self.visit_topic,
                "value_template": "{{ value_json.classification or value_json.outcome }}",
                "json_attributes_topic": self.visit_topic,
                "icon": "mdi:doorbell-video",
                **common,
            },
            f"{self.discovery_prefix}/binary_sensor/{node}/conversation/config": {
                "name": "Conversation",
                "unique_id": f"{node}_conversation",
                "state_topic": self.state_topic,
                "payload_on": "conversation",
                "payload_off": "idle",
                "icon": "mdi:account-voice",
                **common,
            },
            **buttons,
        }

    def _publish(self, topic: str, payload: str) -> None:
        result = self.client.publish(topic, payload, qos=1, retain=True)
        if getattr(result, "rc", 0):
            log.warning("MQTT publish to %s failed: rc=%s", topic, result.rc)
