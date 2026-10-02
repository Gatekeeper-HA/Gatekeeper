import json

from gatekeeper.publish import STATUS_TOPIC, Publisher


class FakeClient:
    def __init__(self):
        self.published = []
        self.will = None

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)


def publisher(client, prefix="homeassistant"):
    return Publisher(client, camera="front_door", discovery_prefix=prefix, version="0.2.0")


def test_will_is_offline_retained():
    client = FakeClient()
    publisher(client).set_will()
    assert client.will == (STATUS_TOPIC, "offline", 1, True)


def test_announce_publishes_status_discovery_and_idle_state():
    client = FakeClient()
    publisher(client).announce()
    topics = [p[0] for p in client.published]
    assert topics == [
        "gatekeeper/status",
        "homeassistant/sensor/gatekeeper_front_door/last_visitor/config",
        "homeassistant/binary_sensor/gatekeeper_front_door/conversation/config",
        "gatekeeper/front_door/state",
    ]
    assert all(p[2] == 1 and p[3] is True for p in client.published)  # qos 1, retained
    assert client.published[0][1] == "online"
    assert client.published[-1][1] == "idle"


def test_discovery_configs_share_a_device_and_availability():
    configs = publisher(FakeClient()).discovery()
    sensor = configs["homeassistant/sensor/gatekeeper_front_door/last_visitor/config"]
    binary = configs["homeassistant/binary_sensor/gatekeeper_front_door/conversation/config"]
    assert sensor["state_topic"] == sensor["json_attributes_topic"] == "gatekeeper/front_door/visit"
    assert sensor["unique_id"] == "gatekeeper_front_door_last_visitor"
    assert binary["state_topic"] == "gatekeeper/front_door/state"
    assert (binary["payload_on"], binary["payload_off"]) == ("conversation", "idle")
    for c in (sensor, binary):
        assert c["availability_topic"] == STATUS_TOPIC
        assert c["device"]["identifiers"] == ["gatekeeper_front_door"]
        assert c["device"]["sw_version"] == "0.2.0"


def test_discovery_can_be_disabled():
    client = FakeClient()
    publisher(client, prefix="").announce()
    assert [p[0] for p in client.published] == ["gatekeeper/status", "gatekeeper/front_door/state"]


def test_visit_and_conversation_state():
    client = FakeClient()
    p = publisher(client)
    p.conversation(True)
    record = {"event_id": "e1", "outcome": "completed", "transcript": "Paquete, señor"}
    p.visit(record)
    p.conversation(False)
    assert client.published[0][:2] == ("gatekeeper/front_door/state", "conversation")
    topic, payload, _, retain = client.published[1]
    assert topic == "gatekeeper/front_door/visit" and retain
    assert json.loads(payload) == record
    assert "señor" in payload  # UTF-8, not \\u escapes
    assert client.published[2][:2] == ("gatekeeper/front_door/state", "idle")
