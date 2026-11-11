"""Run the name service, or manage it.

    python -m hubnames                 serve the API
    python -m hubnames claims 5        print 5 new claim codes
    python -m hubnames remove <id>     take a hub's name down

Settings, from the environment:
    HUBNAMES_ZONE      the zone of hub names (required)
    PDNS_API_URL       PowerDNS's API (default http://pdns:8081)
    PDNS_API_KEY       its key (required)
    HUBNAMES_DB        the database (default /data/hubnames.db)
    HUBNAMES_PORT      the API's port (default 8080)
"""

from __future__ import annotations

import logging
import os
import sys
import threading

from hubnames.api import serve
from hubnames.powerdns import PowerDNS
from hubnames.service import NameService
from hubnames.store import Store

SWEEP_SECONDS = 60.0


def service() -> NameService:
    try:
        zone, key = os.environ["HUBNAMES_ZONE"], os.environ["PDNS_API_KEY"]
    except KeyError as e:
        sys.exit(f"{e.args[0]} isn't set")
    dns = PowerDNS(os.environ.get("PDNS_API_URL", "http://pdns:8081"), key, zone)
    return NameService(Store(os.environ.get("HUBNAMES_DB", "/data/hubnames.db")), dns)


def main(argv: list[str]) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s"
    )
    names = service()
    if argv[:1] == ["claims"] and len(argv) == 2 and argv[1].isdigit():
        print("\n".join(names.store.new_claims(int(argv[1]))))
    elif argv[:1] == ["remove"] and len(argv) == 2:
        names.remove(argv[1])
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
