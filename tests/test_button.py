import asyncio

import pytest

from gatekeeper.button import CALLBACK_ID, DoorbellButton


class FakeBaichuan:
    def __init__(self):
        self.callbacks = {}
        self.subscribed = False
        self.renewals = 0
        self.events_active = False

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


class ScriptedBaichuan(FakeBaichuan):
    """events_active as scripted for each check, then the last value."""

    def __init__(self, script):
        super().__init__()
        self.script = script

    @property
    def events_active(self):
        return self.script[min(self.renewals, len(self.script)) - 1]

    @events_active.setter
    def events_active(self, value):
        pass


def listen(script, checks):
    """Run the listener until it has made ``checks`` checks, or stopped; the outcome."""
    host = FakeHost()
    host.baichuan = ScriptedBaichuan(script)
    button = DoorbellButton("cam", "u", "p", on_press=lambda: None,
                            host_factory=lambda *a: host, renew_interval=0.001)  # fmt: skip

    async def scenario():
        task = asyncio.create_task(button.run())
        while host.baichuan.renewals < checks and not task.done():
            await asyncio.sleep(0.001)
        if task.done():
            return task.exception()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return None

    return asyncio.run(scenario()), host


def test_reconnects_when_push_events_stop_after_coming_in():
    # None yet (a quiet night), then events, then none for two checks: the doorbell
    # restarted.
    error, host = listen([False, False, False, True, True, False, False], checks=20)
    assert isinstance(error, ConnectionError) and "push events stopped" in str(error)
    assert host.baichuan.renewals == 7
    assert host.logged_out and not host.baichuan.subscribed


def test_no_events_yet_or_a_brief_gap_is_not_a_drop():
    # A quiet night: never any events.
    error, _ = listen([False], checks=20)
    assert error is None
    # Inactive for one check only, then back (reolink-aio resubscribed by itself).
    error, _ = listen([True, True, False, True, True, False, True], checks=20)
    assert error is None


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
