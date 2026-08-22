# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS build

WORKDIR /source
COPY . ./

WORKDIR /source/frontend_audit
RUN npm ci --no-audit --no-fund \
    && npm run build


FROM nginx:1.27-alpine AS runtime

ARG SOURCE_SHA

LABEL org.opencontainers.image.title="RADAI university audit frontend test runtime" \
      org.opencontainers.image.revision="${SOURCE_SHA}" \
      io.radai.frontend.kind="audit"

COPY --from=build --chown=nginx:nginx /source/frontend_audit/dist/ /usr/share/nginx/html/
COPY --chown=nginx:nginx .docker-build-assets/nginx/nginx.conf /etc/nginx/nginx.conf
COPY --chown=nginx:nginx .docker-build-assets/nginx/audit.conf.template /opt/radai-nginx/audit.conf.template
COPY --chown=nginx:nginx .docker-build-assets/nginx/proxy-common.conf.template /opt/radai-nginx/proxy-common.conf.template
COPY --chown=nginx:nginx .docker-build-assets/nginx/audit-entrypoint.sh /usr/local/bin/radai-audit-frontend-entrypoint

RUN chmod 0555 /usr/local/bin/radai-audit-frontend-entrypoint \
    && find /usr/share/nginx/html -type d -exec chmod 0555 {} + \
    && find /usr/share/nginx/html -type f -exec chmod 0444 {} +

USER nginx

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/radai-audit-frontend-entrypoint"]
