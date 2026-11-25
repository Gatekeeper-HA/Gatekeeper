"""The relay's own parts that run without SniTun (it needs Python 3.13: relay/check.py tests
the rest in the relay's image)."""

import ipaddress
import re
from pathlib import Path

import pytest

from relay import __main__ as relay

RELAY = Path(__file__).parents[1] / "relay"


class Multiplexer:
    def __init__(self):
        self.closed = False

    def shutdown(self):
        self.closed = True


class Peer:
    def __init__(self, valid, connected=True):
        self.is_valid = valid
        self.multiplexer = Multiplexer() if connected else None


class PeerManager:
    def __init__(self, **peers):
        self._peers = peers


def test_the_sweep_closes_only_tunnels_whose_pass_has_run_out():
    peers = PeerManager(
        fresh=Peer(valid=True), stale=Peer(valid=False), half=Peer(valid=False, connected=False)
    )
    assert relay.sweep(peers) == 1
    assert peers._peers["stale"].multiplexer.closed
    assert not peers._peers["fresh"].multiplexer.closed


def test_the_relay_needs_its_keys(monkeypatch):
    monkeypatch.delenv("RELAY_FERNET_KEYS", raising=False)
    with pytest.raises(SystemExit, match="RELAY_FERNET_KEYS"):
        relay.main()


def denied() -> list[tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]]:
    text = (RELAY / "turnserver.conf").read_text()
    ranges = []
    for first, last in re.findall(r"^denied-peer-ip=(\S+)-(\S+)$", text, re.M):
        ranges.append((ipaddress.ip_address(first), ipaddress.ip_address(last)))
    return ranges


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5", "172.17.0.1", "192.168.1.20", "127.0.0.1", "0.0.0.0",
        "169.254.169.254",  # DigitalOcean's metadata service
        "100.100.100.100",  # Tailscale's
        "224.0.0.1", "255.255.255.255", "192.0.2.1",
        "::1", "fd48:6b6e:7265::2", "fe80::1", "ff02::1", "::ffff:10.0.0.1",
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
        "2002:a00:1::1",  # 6to4 of 10.0.0.1
        "2001:db8::1",
    ],
)  # fmt: skip
def test_turn_relays_to_no_private_or_special_address(address):
    ip = ipaddress.ip_address(address)
    # coturn checks an IPv4 address written as IPv6 against the IPv4 ranges (checked live).
    ip = getattr(ip, "ipv4_mapped", None) or ip
    assert any(type(ip) is type(a) and a <= ip <= b for a, b in denied())


def test_no_ipv6_range_starts_at_the_unspecified_address():
    """coturn 4.18 refuses every IPv4 peer when an IPv6 range starts at ``::`` (checked
    live); ``::`` itself it refuses anyway."""
    assert not [a for a, _ in denied() if a == ipaddress.IPv6Address("::")]


@pytest.mark.parametrize("address", ["8.8.8.8", "203.0.114.1", "2001:4860:4860::8888"])
def test_turn_relays_to_the_internet(address):
    ip = ipaddress.ip_address(address)
    assert not any(type(ip) is type(a) and a <= ip <= b for a, b in denied())


def test_turn_listens_and_relays_only_on_the_public_addresses():
    compose = (RELAY / "compose.yml").read_text()
    for flag in ("listening-ip", "relay-ip", "denied-peer-ip"):
        assert f"--{flag}=${{RELAY_IPV4" in compose and f"--{flag}=${{RELAY_IPV6" in compose
    conf = (RELAY / "turnserver.conf").read_text()
    for line in ("use-auth-secret", "no-tcp-relay", "no-multicast-peers", "no-cli",
                 "log-file=/dev/null"):  # fmt: skip
        assert re.search(rf"^{line}$", conf, re.M)
