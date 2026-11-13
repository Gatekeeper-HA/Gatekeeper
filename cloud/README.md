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
python -m hubnames zone            # set up the zone's own records (Deploying)
```

| Setting | Default | |
|---|---|---|
| `HUBNAMES_ZONE` | (required) | the zone the hub names are in |
| `PDNS_API_KEY` | (required) | PowerDNS's API key |
| `PDNS_API_URL` | `http://pdns:8081` | PowerDNS's API |
| `HUBNAMES_DB` | `/data/hubnames.db` | the hubs and claim codes |
| `HUBNAMES_PORT` | `8080` | the API's port |
| `SERVER_IPV4`, `SERVER_IPV6` | | this server's addresses, for `zone` (IPv6 optional) |

It needs Python 3.11 and `cryptography`. The tests are in `cloud/tests` and run with the
rest (`pytest`).

## Deploying

Gatekeeper's own runs on `gatekeeperhome.net`, with the API at `api.gatekeeperhome.net`.
Registering a hub there needs a claim code from us, so it isn't open to anyone's use. To run
your own, follow these steps with a domain of your own.

One small server with a fixed IPv4 and IPv6 address runs everything (`compose.yml`):

- **PowerDNS** answers for the zone, on port 53.
- **hubnames** writes the hubs' records through PowerDNS's API.
- **Caddy** gives the API HTTPS (`api.<zone>`), with a Let's Encrypt certificate.

Only ports 53, 80 and 443 are open to the internet. PowerDNS's API and hubnames' own port
stay on the compose network. 1 CPU and 1 GB of memory is plenty.

**How the zone finds its server.** The zone's nameservers, `ns1.<zone>` and `ns2.<zone>`, are
inside the zone they answer for. So the registry also keeps their addresses, the **glue
records**, and hands them out with the referral. Without them a lookup would go round in
circles.

### 1. The server

On a new Ubuntu 24.04 server, as root:

```sh
sh setup-server.sh <admin user>
reboot
```

It does the following:
- adds the admin user, with root's SSH keys, sudo and Docker;
- turns off password and root logins over SSH;
- sets up a firewall (SSH, DNS and HTTP(S) only);
- turns on automatic security updates, rebooting at 04:00 (the server's time) when an update
  needs it;
- installs Docker, with its logs capped.

The first updates usually include a new kernel, hence the reboot. Log in as the admin user
from then on.

### 2. The services

As the admin user:

```sh
git clone https://github.com/Gatekeeper-HA/Gatekeeper && cd Gatekeeper/cloud
cp .env.example .env && chmod 600 .env    # then fill it in
docker compose up -d --build
docker compose exec hubnames python -m hubnames zone
```

`zone` creates the zone and sets its own records:

| Name | Records |
|---|---|
| the zone | SOA; NS `ns1` and `ns2` |
| `ns1`, `ns2`, `api` | A and AAAA: this server's addresses |
| the zone | CAA: Let's Encrypt only, no wildcards |
| the zone, `_dmarc` | No email: a null MX, SPF `-all` and DMARC `reject`, so nobody can send phishing in the zone's name |

It's safe to run again after an address change.

The DNS server already answers before the domain points at it, so check it directly:

```sh
dig +norec @<server's IPv4> <zone> NS        # ns1 and ns2, with the "aa" flag
dig +norec @<server's IPv4> api.<zone> A     # the server's address
dig +norec @<server's IPv4> example.com A    # REFUSED: it only answers for the zone
```

### 3. The domain

At the registrar, set two things, in this order:
1. **Glue records** for `ns1` and `ns2`, each with the server's IPv4 and IPv6 address. Most
   registrars keep these apart from their DNS editor (for example "Glue records", "Host
   names" or "Child nameservers"). Records added in the DNS editor don't count.
2. **Nameservers:** replace the registrar's own with `ns1.<zone>` and `ns2.<zone>`.

The registrar has to let you use your own nameservers. Cloudflare Registrar, for one,
doesn't. Changes usually show within minutes:

```sh
dig @a.gtld-servers.net <zone> NS            # the registry's referral and glue (.com/.net)
dig +short NS <zone> @1.1.1.1                # a public resolver
curl https://api.<zone>/v1/health            # {"ok": true}
```

Caddy gets the API's certificate by itself once the name resolves. Its attempts before then
fail, and it backs off; `docker compose restart caddy` makes it try again at once.

### Running it

- **Claim codes:** `docker compose exec hubnames python -m hubnames claims 1`.
- **Updating:** `git pull && docker compose up -d --build`, then `zone` if the zone's own
  records changed. Nothing deploys the server by itself.
- **Reboots:** every service restarts by itself, including after the nightly update reboot.
- **Logs:** `docker compose logs --tail 50 <service>`.
- **Backups:** the hubs are in the `hubnames` volume (`hubnames.db`), the records in the
  `pdns` volume. Losing them would cut every hub off from its name, so back up the server.
- **Moving the server:**
  1. Copy the volumes over.
  2. Update `.env` and run `zone`.
  3. Change the glue records.
  4. Keep the old server running for 2 days, the TTL of the glue at the .com/.net registry.

**Later:** a second nameserver on another server (`ns2`), before customers depend on it.
