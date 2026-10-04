import asyncio

import pytest

from gatekeeper.button import CALLBACK_ID, DoorbellButton


class FakeBaichuan:
    def __init__(self):
        self.callbacks = {}
        self.subscribed = False
        self.renewals = 0

    def register_callback(self, callback_id, callback, cmd_id=None, channel=None):
        self.callbacks[callback_id] = callback

    def unregister_callback(self, callback_id):
        self.callbacks.pop(callback_id, None)

    async def subscribe_events(self):
        self.subscribed = True

    async def unsubscribe_events(self):
        self.subscribed = False

    async def check_subscribe_events(self):
        self.renewals += 1


class FakeHost:
    model = "Reolink Video Doorbell WiFi"
    sw_version = "v3.0.0"

    def __init__(self, doorbell=True):
        self.baichuan = FakeBaichuan()
        self.visitor = False
        self.doorbell = doorbell
        self.logged_out = False

    async def get_host_data(self):
        pass

    def is_doorbell(self, channel):
        return self.doorbell

    def visitor_detected(self, channel):
        return self.visitor

    async def logout(self):
        self.logged_out = True


def push(host, visitor):
    host.visitor = visitor
    host.baichuan.callbacks[CALLBACK_ID]()


def test_press_is_the_rising_edge_of_the_visitor_state():
    presses = []
    host = FakeHost()
    button = DoorbellButton("cam", "u", "p", on_press=lambda: presses.append(1),
                            host_factory=lambda *a: host, renew_interval=0.01)  # fmt: skip

    async def scenario():
        task = asyncio.create_task(button.run())
        await asyncio.sleep(0.05)
        assert host.baichuan.subscribed
        push(host, True)   # pressed
        push(host, True)   # still on: same press
        push(host, False)  # released
        push(host, False)  # unrelated event (e.g. motion)
        push(host, True)   # second press
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert presses == [1, 1]
    assert host.baichuan.renewals >= 1
    assert host.logged_out and not host.baichuan.subscribed
    assert CALLBACK_ID not in host.baichuan.callbacks


def test_handler_errors_do_not_stop_listening():
    host = FakeHost()

    def boom():
        raise RuntimeError("tracker bug")

    button = DoorbellButton("cam", "u", "p", on_press=boom, host_factory=lambda *a: host)
    button.check(host)  # visitor False
    host.visitor = True
    button.check(host)  # raises inside, logged


def test_not_a_doorbell_disables_presses():
    host = FakeHost(doorbell=False)
    button = DoorbellButton("cam", "u", "p", on_press=lambda: None, host_factory=lambda *a: host)
    button.run_forever()  # returns instead of retrying forever
    assert button.disabled
    assert host.logged_out
