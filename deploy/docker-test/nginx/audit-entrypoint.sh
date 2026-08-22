#!/bin/sh
set -eu

files_proxy_target="${FILES_PROXY_TARGET:-}"
if [ -z "${files_proxy_target}" ]; then
    echo "FILES_PROXY_TARGET is required" >&2
    exit 1
fi

mkdir -p \
    /tmp/client_temp \
    /tmp/proxy_temp \
    /tmp/fastcgi_temp \
    /tmp/uwsgi_temp \
    /tmp/scgi_temp

export FILES_PROXY_TARGET="${files_proxy_target}"
envsubst '${FILES_PROXY_TARGET}' \
    </opt/radai-nginx/audit.conf.template \
    >/tmp/radai-server.conf

exec nginx -g 'daemon off;'
