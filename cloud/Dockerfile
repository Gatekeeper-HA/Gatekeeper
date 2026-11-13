FROM python:3.11.16-slim-bookworm

# cryptography: the hubs' Ed25519 signatures (the same version as the hub's image).
RUN pip install --no-cache-dir cryptography==50.0.2 \
    && useradd --system --uid 999 --home-dir /data hubnames \
    && install -d -o hubnames -m 700 /data

COPY hubnames /app/hubnames
WORKDIR /app
USER hubnames
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "hubnames"]
