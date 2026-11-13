"""The zone's records, through PowerDNS Authoritative's API.

https://doc.powerdns.com/authoritative/http-api/zone.html: one PATCH replaces or deletes
whole record sets (a name and a type).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class PowerDNS:
    def __init__(self, url: str, key: str, zone: str, timeout: float = 10.0) -> None:
        self.zone = zone.rstrip(".") + "."
        self._zones = f"{url.rstrip('/')}/api/v1/servers/localhost/zones"
        self._url = f"{self._zones}/{self.zone}"
        self._key = key
        self._timeout = timeout

    def _name(self, name: str) -> str:
        return self.zone if name == "@" else f"{name}.{self.zone}"

    def _send(self, method: str, url: str, data: dict | None = None) -> None:
        request = urllib.request.Request(
            url,
            data=json.dumps(data).encode() if data is not None else None,
            method=method,
            headers={"X-API-Key": self._key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self._timeout):
            pass

    def create(self, records: list[tuple[str, str, int, list[str]]]) -> bool:
        """Create the zone with ``records`` (its SOA and NS), unless it's there already:
        False then. Native: PowerDNS serves it, with no zone transfers yet."""
        try:
            self._send("GET", self._url)
            return False
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        rrsets = [
            {
                "name": self._name(name),
                "type": kind,
                "ttl": ttl,
                "records": [{"content": c, "disabled": False} for c in contents],
            }
            for name, kind, ttl, contents in records
        ]
        zone = {"name": self.zone, "kind": "Native", "soa_edit_api": "DEFAULT"}
        self._send("POST", self._zones, zone | {"nameservers": [], "rrsets": rrsets})
        return True

    def change(self, records: list[tuple[str, str, int, list[str]]]) -> None:
        """Set each (name, type, ttl, contents) record set; no contents deletes it. Names
        are relative to the zone ("@" for the zone itself). Raises OSError when PowerDNS
        can't be reached or refuses."""
        rrsets = []
        for name, kind, ttl, contents in records:
            rrset = {"name": self._name(name), "type": kind}
            if contents:
                rrset |= {
                    "changetype": "REPLACE",
                    "ttl": ttl,
                    "records": [{"content": c, "disabled": False} for c in contents],
                }
            else:
                rrset["changetype"] = "DELETE"
            rrsets.append(rrset)
        self._send("PATCH", self._url, {"rrsets": rrsets})
