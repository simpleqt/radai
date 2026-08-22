#!/bin/sh
set -eu

files_proxy_target="${FILES_PROXY_TARGET:-}"
if [ -z "${files_proxy_target}" ]; then
    echo "FILES_PROXY_TARGET is required" >&2
    exit 1
fi

case "${FRONTEND_KIND:-app}" in
    app)
        template=/opt/radai-nginx/app.conf.template
        ;;
    fmca)
        template=/opt/radai-nginx/fmca.conf.template
        ;;
    guest)
        template=/opt/radai-nginx/guest.conf.template
        ;;
    *)
        echo "unsupported FRONTEND_KIND: ${FRONTEND_KIND}" >&2
        exit 1
        ;;
esac

mkdir -p \
    /tmp/client_temp \
    /tmp/proxy_temp \
    /tmp/fastcgi_temp \
    /tmp/uwsgi_temp \
    /tmp/scgi_temp

export FILES_PROXY_TARGET="${files_proxy_target}"
envsubst '${FILES_PROXY_TARGET}' <"${template}" >/tmp/radai-server.conf

exec nginx -g 'daemon off;'
