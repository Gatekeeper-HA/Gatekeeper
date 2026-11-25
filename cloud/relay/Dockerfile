FROM python:3.13.18-slim-bookworm

# Its packages (requirements.txt), kept current by Dependabot; CI runs relay/check.py on
# each new version.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && useradd --system --uid 999 --no-create-home relay

COPY __init__.py __main__.py check.py /app/relay/
WORKDIR /app
USER relay
EXPOSE 8443
CMD ["python", "-m", "relay"]
