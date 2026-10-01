FROM nginx:1.27-alpine
RUN apk add --no-cache openssl
COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY 40-certificates.sh /docker-entrypoint.d/40-certificates.sh
RUN chmod +x /docker-entrypoint.d/40-certificates.sh
