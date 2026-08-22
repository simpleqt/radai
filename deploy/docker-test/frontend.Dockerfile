# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS build

ARG FRONTEND_DIR

WORKDIR /source
COPY . ./

# The guest Vite project aliases `@` to ../frontend/src. Node resolves imports
# from that source tree, so reproduce the host release contract by installing
# the main frontend's locked dependencies before the guest build.
RUN if [ "${FRONTEND_DIR}" = "guest" ]; then \
      cd /source/frontend \
      && npm ci --no-audit --no-fund; \
    fi

WORKDIR /source/${FRONTEND_DIR}
RUN npm ci --no-audit --no-fund \
    && npm run build


FROM nginx:1.27-alpine AS runtime

ARG FRONTEND_DIR
ARG FRONTEND_KIND=app
ARG SOURCE_SHA

LABEL org.opencontainers.image.title="RADAI frontend test runtime" \
      org.opencontainers.image.revision="${SOURCE_SHA}" \
      io.radai.frontend.kind="${FRONTEND_KIND}"

ENV FRONTEND_KIND=${FRONTEND_KIND}

COPY --from=build --chown=nginx:nginx /source/${FRONTEND_DIR}/dist/ /usr/share/nginx/html/
COPY --chown=nginx:nginx .docker-build-assets/nginx/nginx.conf /etc/nginx/nginx.conf
COPY --chown=nginx:nginx .docker-build-assets/nginx/*.conf.template /opt/radai-nginx/
COPY --chown=nginx:nginx .docker-build-assets/nginx/docker-entrypoint.sh /usr/local/bin/radai-frontend-entrypoint

RUN chmod 0555 /usr/local/bin/radai-frontend-entrypoint \
    && find /usr/share/nginx/html -type d -exec chmod 0555 {} + \
    && find /usr/share/nginx/html -type f -exec chmod 0444 {} +

USER nginx

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/radai-frontend-entrypoint"]
