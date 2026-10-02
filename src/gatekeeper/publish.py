"""Publishing visits over MQTT, with Home Assistant MQTT discovery.

Topics (all retained):

- ``gatekeeper/status``: ``online``, or ``offline`` (the client's last will).
- ``gatekeeper/<camera>/state``: ``conversation`` while a visit runs, else ``idle``.
- ``gatekeeper/<camera>/visit``: the latest visit record (JSON, as in events.jsonl).

Discovery gives Home Assistant a "Last visitor" sensor (classification, or the
outcome if the visitor was never greeted, with the record as attributes) and a
"Conversation" binary sensor, grouped under one device per camera.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

STATUS_TOPIC = "gatekeeper/status"


class Publisher:
    def __init__(self, client, *, camera: str, discovery_prefix: str, version: str) -> None:
        self.client = client
        self.camera = camera
        self.discovery_prefix = discovery_prefix
        self.version = version
        self.state_topic = f"gatekeeper/{camera}/state"
        self.visit_topic = f"gatekeeper/{camera}/visit"

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
        }

    def _publish(self, topic: str, payload: str) -> None:
        result = self.client.publish(topic, payload, qos=1, retain=True)
        if getattr(result, "rc", 0):
            log.warning("MQTT publish to %s failed: rc=%s", topic, result.rc)
