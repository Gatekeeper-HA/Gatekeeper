# Gatekeeper's cloud services

What runs on Gatekeeper's servers rather than in the home. It's here, in the open, so anyone
can check what it does with a hub's traffic, and run their own with their own domain. The
hub works without any of it.

## The name service (`hubnames`)

Each hub gets a name of its own, `<id>.<zone>`: 10 random characters, never anything about
the home, since every certificate's names are published in the Certificate Transparency
logs. With it the app installs from an address that has a Let's Encrypt certificate, which
the phone trusts, instead of the hub's self-signed one.

The hub makes its own keys and its own Let's Encrypt account. The service only publishes,
in the zone's DNS:

- **`<id>`, A:** the hub's address on the home network. Private addresses only (10.x,
  172.16–31.x, 192.168.x), so a name can't point at a public site.
- **`_acme-challenge.<id>`, TXT:** the DNS-01 challenge while the hub gets its certificate.
  It's removed after 10 minutes.
- **`<id>`, CAA:** only the hub's own Let's Encrypt account may get certificates for its
  name, by DNS-01 only, and no wildcard (RFC 8657).

A hub registers once with a **claim code**, a one-time code we issue, and its Ed25519
public key. Every later request is signed with that key, so the service keeps only public
keys. Each hub may make 60 changes an hour.

### The API

It runs behind a proxy that gives it HTTPS.

| Request | Body | Answer |
|---|---|---|
| `GET /v1/health` | | `{"ok": true}` |
| `POST /v1/hubs` | `{"claim": "ABCD-EFGH-JKLM", "key": <public key, base64url>}` | 201 `{"id": ..., "name": ...}` |
| `PUT /v1/hubs/<id>/address` | `{"address": "192.168.1.20"}` | 204 |
| `PUT /v1/hubs/<id>/challenge` | `{"values": [<TXT value>, ...]}` (at most 2; none removes them) | 204 |
| `PUT /v1/hubs/<id>/caa` | `{"account": <its Let's Encrypt account URL>}` | 204 |
| `DELETE /v1/hubs/<id>` | | 204 |

**Signing:** `Authorization: Hub <unix time>.<signature>`. The signature (Ed25519,
base64url) is over `"<METHOD> <path> <unix time> <SHA-256 of the body, hex>"`. Registration
is signed with the key being registered. A signature is good for 5 minutes either side of
the service's clock.

**Errors:** `{"error": ...}`, with:
- 400 for a bad body;
- 403 for an unknown or used claim code, or a bad signature;
- 404 for an unknown hub;
- 429 past 60 changes an hour;
- 502 when PowerDNS can't be reached.

### Running it

The zone's DNS server is [PowerDNS Authoritative](https://doc.powerdns.com/authoritative/),
with its API on. `hubnames` writes the records through that API and keeps the hubs in SQLite.

```sh
HUBNAMES_ZONE=<the zone> PDNS_API_KEY=<PowerDNS's API key> python -m hubnames
python -m hubnames claims 5        # 5 new claim codes, printed once (only hashes are kept)
python -m hubnames remove <id>     # take a hub's name down
```

| Setting | Default | |
|---|---|---|
| `HUBNAMES_ZONE` | (required) | the zone the hub names are in |
| `PDNS_API_KEY` | (required) | PowerDNS's API key |
| `PDNS_API_URL` | `http://pdns:8081` | PowerDNS's API |
| `HUBNAMES_DB` | `/data/hubnames.db` | the hubs and claim codes |
| `HUBNAMES_PORT` | `8080` | the API's port |

It needs Python 3.11 and `cryptography`. The tests are in `cloud/tests` and run with the
rest (`pytest`).
