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
| `POST /v1/hubs/<id>/tunnel` | `{}` | 200: a pass for the relay ([The relay](#the-relay)) |
| `POST /v1/hubs/<id>/turn` | `{}` | 200: TURN credentials for Live |
| `DELETE /v1/hubs/<id>` | | 204 |

**Signing:** `Authorization: Hub <unix time>.<signature>`. The signature (Ed25519,
base64url) is over `"<METHOD> <path> <unix time> <SHA-256 of the body, hex>"`. Registration
is signed with the key being registered. A signature is good for 5 minutes either side of
the service's clock.

**Errors:** `{"error": ...}`, with:
- 400 for a bad body;
- 403 for an unknown or used claim code, a bad signature, or a pass for a hub without
  Cloud Plus;
- 404 for an unknown hub;
- 429 past 60 changes an hour, or for an address whose requests were refused 20 times in
  the last hour (any 400, 403 or 404), with `Retry-After`;
- 502 when PowerDNS can't be reached.

The address is the last one in `X-Forwarded-For`, which Caddy sets. Only Caddy can reach
the API, so it can't be forged. Only internet addresses are counted.

`compose.yml` gives its network IPv6, so Docker forwards IPv6 clients to Caddy directly, as
it does IPv4 ones. Without that, Docker's proxy would carry them, and every IPv6 client would
show up as one private address.

### Running it

The zone's DNS server is [PowerDNS Authoritative](https://doc.powerdns.com/authoritative/),
with its API on. `hubnames` writes the records through that API and keeps the hubs in SQLite.

```sh
HUBNAMES_ZONE=<the zone> PDNS_API_KEY=<PowerDNS's API key> python -m hubnames
python -m hubnames claims 5        # 5 new claim codes, printed once (only hashes are kept)
python -m hubnames remove <id>     # take a hub's name down
python -m hubnames zone            # set up the zone's own records (Deploying)
python -m hubnames plus            # the hubs with Cloud Plus, and until when
python -m hubnames plus <id> 90    # Cloud Plus for 90 more days; `off` ends it
```

| Setting | Default | |
|---|---|---|
| `HUBNAMES_ZONE` | (required) | the zone the hub names are in |
| `PDNS_API_KEY` | (required) | PowerDNS's API key |
| `PDNS_API_URL` | `http://pdns:8081` | PowerDNS's API |
| `HUBNAMES_DB` | `/data/hubnames.db` | the hubs and claim codes |
| `HUBNAMES_PORT` | `8080` | the API's port |
| `SERVER_IPV4`, `SERVER_IPV6` | | this server's addresses, for `zone` (IPv6 optional) |
| `RELAY_IPV4`, `RELAY_IPV6` | | the relay's addresses, for `relay.<zone>` (`zone`) |
| `RELAY_FERNET_KEYS` | | the keys shared with the relay, comma-separated; the first makes new passes. Without them, no passes |
| `TURN_SECRET` | | coturn's secret, shared with the relay |

It needs Python 3.11 and `cryptography`. The tests are in `cloud/tests` and run with the
rest (`pytest`).

### The relay

With Cloud Plus, the app also works away from home. The hub keeps a tunnel open to the relay,
[SniTun](https://github.com/NabuCasa/snitun), the relay behind Home Assistant Cloud. A phone
connects to the hub's name, which points at the relay while the tunnel is up. The relay reads
only the name in the TLS hello and passes the still-encrypted connection down that hub's
tunnel, so TLS ends on the hub. The relay never holds a key for a hub's name.

The name service decides who may use it:
- **Cloud Plus** is a date per hub (`python -m hubnames plus`), set by hand until there's
  billing.
- **A pass** (`POST /v1/hubs/<id>/tunnel`, signed, Cloud Plus only) is a Fernet token that
  only the relay can read:
  - it names the hub's own name, and nothing else;
  - it holds a new AES key, so the hub proves it's the one that asked;
  - it sets SniTun's cipher, AES-GCM-SIV, and protocol 2, which keeps the phone's address
    encrypted in the tunnel;
  - it lasts an hour.

  The hub asks for a new pass each time it connects, so a key is never used for two
  connections.
- **TURN credentials** (`POST /v1/hubs/<id>/turn`) let Live work away from home when no
  direct path does. They're coturn's REST format, last 4 hours, and are made with a secret
  that never leaves our servers.

The relay itself runs on a server of its own ([4. The relay](#4-the-relay-optional)), with
two parts:
- **SniTun's server** on port 443 (`relay/`). It takes the hubs' tunnels and the phones'
  connections on the same port, telling them apart by their first bytes. It adds one thing to
  SniTun: every minute it closes tunnels whose pass has run out, since SniTun checks a pass
  only when its tunnel connects. It keeps no logs of names or addresses, just a count of open
  tunnels.
- **coturn**, for TURN, on port 3478 with media on 49152–49999. It relays only to internet
  addresses: never to private or special ones (`turnserver.conf`), nor to the server's own.
  So it can't be used to reach a network, or the cloud provider's metadata service. It keeps
  no session logs.

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

### 4. The relay (optional)

On a second small server (Ubuntu 24.04, a fixed IPv4 and IPv6), set up as in step 1. Then:

1. **The firewall:** the relay needs no DNS and no plain HTTP, but it does need TURN.
   ```sh
   sudo ufw delete allow 53 && sudo ufw delete allow 80/tcp
   sudo ufw allow 3478 && sudo ufw allow 49152:49999/udp
   ```
2. **The secrets,** made on the name service's server, go in both servers' `.env`:
   - `RELAY_FERNET_KEYS`, made with
     `python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'`;
   - `TURN_SECRET`, made with `openssl rand -hex 32`.
3. **The relay**, as the admin user:
   ```sh
   git clone https://github.com/Gatekeeper-HA/Gatekeeper && cd Gatekeeper/cloud/relay
   cp .env.example .env && chmod 600 .env    # ZONE, the relay's addresses, the two secrets
   docker compose up -d --build
   ```
4. **The name service:** add the relay's addresses and the two secrets to its `.env`, then
   `docker compose up -d` and `docker compose exec hubnames python -m hubnames zone`
   (`relay.<zone>`).
5. **A hub:** `python -m hubnames plus <id> 90`.

Before a new SniTun version: `docker compose build` with it, then run
`docker run --rm -v "$PWD/..:/cloud:ro" -e PYTHONPATH=/cloud relay-snitun python -m
relay.check`. The sweep reads SniTun's own tunnel list, so this checks that it still does
(CI runs the same check).

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
