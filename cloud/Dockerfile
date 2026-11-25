FROM python:3.13.18-slim-bookworm

# Its packages (requirements.txt), kept current by Dependabot.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && useradd --system --uid 999 --home-dir /data hubnames \
    && install -d -o hubnames -m 700 /data

COPY hubnames /app/hubnames
WORKDIR /app
USER hubnames
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "hubnames"]
