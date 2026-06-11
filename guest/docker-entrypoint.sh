#!/bin/sh
set -eu

: "${PORT:=8084}"
: "${BACKEND_URL:=http://localhost:8083}"

envsubst '${PORT} ${BACKEND_URL}' \
  < /etc/nginx/templates/default.conf.template \
  > /etc/nginx/conf.d/default.conf

exec nginx -g 'daemon off;'
