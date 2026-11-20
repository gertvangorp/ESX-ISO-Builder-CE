FROM python:3.13-slim

# xorriso builds the ISOs, openssl hashes the root password (SHA-512 crypt)
RUN apt-get update \
 && apt-get install -y --no-install-recommends xorriso openssl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --home /app --shell /usr/sbin/nologin builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY seed/ seed/
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh && mkdir -p /data && chown builder:builder /data

USER 10001
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 PORT=8443
EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import os,ssl,urllib.request as u; c=ssl._create_unverified_context(); s='https' if os.path.exists('/certs/server.crt') else 'http'; u.urlopen(f'{s}://127.0.0.1:{os.environ[\"PORT\"]}/healthz', context=c if s=='https' else None, timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
