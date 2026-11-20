#!/bin/sh
# HTTPS when a certificate is mounted in /certs (leaf + intermediates + key from your CA), plain HTTP otherwise.
# MEDIA_HTTP_PORT set -> a second, media-only listener on plain HTTP (only /media/*.iso + /healthz) for fast
# iDRAC virtual media. Same media login; restrict it to the iDRAC subnet in the firewall.
set -e
cd /app/app
if [ -n "$MEDIA_HTTP_PORT" ]; then
  echo "Media-only HTTP listener on :$MEDIA_HTTP_PORT"
  MEDIA_ONLY=1 uvicorn main:app --host 0.0.0.0 --port "$MEDIA_HTTP_PORT" &
fi
if [ -s /certs/server.crt ] && [ -s /certs/server.key ]; then
  echo "TLS: /certs/server.crt"
  exec uvicorn main:app --host 0.0.0.0 --port "$PORT" --proxy-headers \
       --ssl-certfile /certs/server.crt --ssl-keyfile /certs/server.key
fi
echo "WARNING: no certificate in /certs - serving plain HTTP"
exec uvicorn main:app --host 0.0.0.0 --port "$PORT" --proxy-headers