FROM python:3.13.18-slim-bookworm

# SniTun, the relay behind Home Assistant Cloud (it needs Python 3.13). Pinned: the sweep
# (__main__.py) reads its PeerManager, so a new version is checked with `python -m
# relay.check` first (cloud/README.md, "The relay").
RUN pip install --no-cache-dir snitun==0.47.0 cryptography==50.0.2 \
    && useradd --system --uid 999 --no-create-home relay

COPY __init__.py __main__.py check.py /app/relay/
WORKDIR /app
USER relay
EXPOSE 8443
CMD ["python", "-m", "relay"]
