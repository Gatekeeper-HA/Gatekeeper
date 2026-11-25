"""The relay (cloud/README.md, "The relay"): SniTun's server, for hubs with Cloud Plus.

A hub keeps a tunnel open here with a pass from the name service (``POST
/v1/hubs/<id>/tunnel``). A phone connects to the hub's name, which points here, and SniTun
passes its still-encrypted connection down that hub's tunnel: TLS ends on the hub. Tunnels
and phones arrive on the same port; SniTun tells them apart by their first bytes.

What this adds to SniTun (``__main__``): closing tunnels whose pass has run out, since SniTun
checks a pass only when its tunnel connects, and a count of open tunnels in the log, without
their names.
"""
