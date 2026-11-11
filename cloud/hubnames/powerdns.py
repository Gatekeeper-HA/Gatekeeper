"""The zone's records, through PowerDNS Authoritative's API.

https://doc.powerdns.com/authoritative/http-api/zone.html: one PATCH replaces or deletes
whole record sets (a name and a type).
"""

from __future__ import annotations

import json
import urllib.request


class PowerDNS:
    def __init__(self, url: str, key: str, zone: str, timeout: float = 10.0) -> None:
        self.zone = zone.rstrip(".") + "."
        self._url = f"{url.rstrip('/')}/api/v1/servers/localhost/zones/{self.zone}"
        self._key = key
        self._timeout = timeout

    def change(self, records: list[tuple[str, str, int, list[str]]]) -> None:
        """Set each (name, type, ttl, contents) record set; no contents deletes it. Names
        are relative to the zone. Raises OSError when PowerDNS can't be reached or refuses."""
        rrsets = []
        for name, kind, ttl, contents in records:
            rrset = {"name": f"{name}.{self.zone}", "type": kind}
            if contents:
                rrset |= {
                    "changetype": "REPLACE",
                    "ttl": ttl,
                    "records": [{"content": c, "disabled": False} for c in contents],
                }
            else:
                rrset["changetype"] = "DELETE"
            rrsets.append(rrset)
        request = urllib.request.Request(
            self._url,
            data=json.dumps({"rrsets": rrsets}).encode(),
            method="PATCH",
            headers={"X-API-Key": self._key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self._timeout):
            pass
