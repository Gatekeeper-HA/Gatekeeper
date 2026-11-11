"""The name service for Gatekeeper hubs.

Each hub gets a name of its own, ``<id>.<zone>``, so the phone app installs from an address
with a certificate the phone trusts. The service publishes, in the zone's DNS:

- the hub's address on the home network (private addresses only);
- the TXT record of Let's Encrypt's DNS-01 challenge, while the hub gets its certificate;
- a CAA record that lets only the hub's own ACME account get certificates for its name.

The hub makes its keys and keeps them; this keeps only the public half of its signing key.
The DNS itself is PowerDNS Authoritative, written to through its API.
"""
