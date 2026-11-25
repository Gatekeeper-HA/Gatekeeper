"""Run the name service, or manage it.

    python -m hubnames                 serve the API
    python -m hubnames zone            set up the zone's own records (again)
    python -m hubnames claims 5        print 5 new claim codes
    python -m hubnames remove <id>     take a hub's name down
    python -m hubnames plus            the hubs with Cloud Plus, and until when
    python -m hubnames plus <id> 90    Cloud Plus for 90 more days (from its end, if later)
    python -m hubnames plus <id> off   no Cloud Plus

Settings, from the environment:
    HUBNAMES_ZONE      the zone of hub names (required)
    PDNS_API_URL       PowerDNS's API (default http://pdns:8081)
    PDNS_API_KEY       its key (required)
    HUBNAMES_DB        the database (default /data/hubnames.db)
    HUBNAMES_PORT      the API's port (default 8080)
    SERVER_IPV4        this server's addresses, for the zone's nameservers and the API
    SERVER_IPV6        (for ``zone``; IPv6 is optional)
    RELAY_IPV4         the relay's addresses, for ``relay.<zone>`` (for ``zone``; optional)
    RELAY_IPV6
    RELAY_FERNET_KEYS  the keys shared with the relay, comma-separated (the first makes new
                       passes); without them there are no relay passes
    TURN_SECRET        coturn's secret, for Live's TURN credentials
"""

from __future__ import annotations

import datetime
import logging
import os
import sys
import threading
import time

from hubnames.api import serve
from hubnames.powerdns import PowerDNS
from hubnames.service import NameService, Relay, zone_records
from hubnames.store import Store

SWEEP_SECONDS = 60.0


def service() -> NameService:
    try:
        zone, key = os.environ["HUBNAMES_ZONE"], os.environ["PDNS_API_KEY"]
    except KeyError as e:
        sys.exit(f"{e.args[0]} isn't set")
    dns = PowerDNS(os.environ.get("PDNS_API_URL", "http://pdns:8081"), key, zone)
    relay = None
    if keys := [k.strip() for k in os.environ.get("RELAY_FERNET_KEYS", "").split(",") if k.strip()]:
        secret = os.environ.get("TURN_SECRET", "")
        if not secret:
            sys.exit("TURN_SECRET isn't set (it goes with RELAY_FERNET_KEYS)")
        relay = Relay(
            f"relay.{zone.rstrip('.')}",
            tuple(keys),
            secret,
            ipv4=os.environ.get("RELAY_IPV4", ""),
            ipv6=os.environ.get("RELAY_IPV6", ""),
        )
    store = Store(os.environ.get("HUBNAMES_DB", "/data/hubnames.db"))
    return NameService(store, dns, relay=relay)


def _day(when: float) -> str:
    return datetime.datetime.fromtimestamp(when, datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")


def plus(names: NameService, argv: list[str]) -> None:
    """``plus``, ``plus <id> <days>`` or ``plus <id> off``."""
    if not argv:
        for hub_id, until in names.store.plus_hubs():
            ended = " (ended)" if until <= time.time() else ""
            print(f"{hub_id}  until {_day(until)}{ended}")
        return
    hub_id, what = argv
    if names.store.hub(hub_id) is None:
        sys.exit(f"no hub {hub_id}")
    if what == "off":
        names.store.set_plus(hub_id, None)
        print(f"{hub_id}: no Cloud Plus")
        return
    if not what.isdigit() or not 0 < int(what) <= 3660:
        sys.exit("plus <id> <days>: 1 to 3660 days, or off")
    start = max(time.time(), names.store.plus_until(hub_id) or 0.0)
    until = start + int(what) * 86400
    names.store.set_plus(hub_id, until)
    print(f"{hub_id}: Cloud Plus until {_day(until)}")


def main(argv: list[str]) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s"
    )
    names = service()
    if argv[:1] == ["claims"] and len(argv) == 2 and argv[1].isdigit():
        print("\n".join(names.store.new_claims(int(argv[1]))))
    elif argv[:1] == ["remove"] and len(argv) == 2:
        names.remove(argv[1])
    elif argv[:1] == ["plus"] and len(argv) in (1, 3):
        plus(names, argv[1:])
    elif argv == ["zone"]:
        if "SERVER_IPV4" not in os.environ:
            sys.exit("SERVER_IPV4 isn't set")
        records = zone_records(
            names.zone,
            os.environ["SERVER_IPV4"],
            os.environ.get("SERVER_IPV6", ""),
            os.environ.get("RELAY_IPV4", ""),
            os.environ.get("RELAY_IPV6", ""),
        )
        if names.dns.create([r for r in records if r[1] in ("SOA", "NS")]):
            print(f"created {names.zone}")
        names.dns.change([r for r in records if r[1] != "SOA"])
        print(f"{names.zone}'s own records are set")
    elif not argv:
        port = int(os.environ.get("HUBNAMES_PORT", "8080"))
        serve(names, port=port)
        logging.getLogger("hubnames").info("serving %s on :%d", names.zone, port)
        stop = threading.Event()
        while not stop.wait(SWEEP_SECONDS):
            names.sweep()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
