#!/bin/sh
# nginx needs a certificate before Let's Encrypt has issued one. Start with a self-signed
# placeholder, switch to the real certificate once certbot creates it, and reload on renewal.
set -eu
CERTS=/etc/nginx/certs
LIVE="/etc/letsencrypt/live/${DOMAIN}"
mkdir -p "$CERTS" /var/www/certbot

link_certificates() {
    if [ -f "$LIVE/fullchain.pem" ]; then
        ln -sf "$LIVE/fullchain.pem" "$CERTS/fullchain.pem"
        ln -sf "$LIVE/privkey.pem" "$CERTS/privkey.pem"
        return 0
    fi
    return 1
}

if ! link_certificates; then
    openssl req -x509 -nodes -newkey rsa:2048 -days 7 -subj "/CN=${DOMAIN}" \
        -keyout "$CERTS/privkey.pem" -out "$CERTS/fullchain.pem" >/dev/null 2>&1
    echo "Using a temporary self-signed certificate for ${DOMAIN}"
fi

(
    while :; do
        sleep 300
        before=$(readlink "$CERTS/fullchain.pem" 2>/dev/null || echo self-signed)
        if link_certificates; then
            # Reload when switching from the placeholder and periodically to pick up renewals.
            if [ "$before" = self-signed ] || [ "$(date +%H%M)" -lt 0005 ]; then
                nginx -s reload && echo "nginx reloaded with Let's Encrypt certificate"
            fi
        fi
    done
) &
