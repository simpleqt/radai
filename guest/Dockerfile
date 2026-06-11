FROM node:20-alpine AS builder

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .
RUN ln -sfn /app/frontend /frontend
RUN npm run build

FROM nginx:1.27-alpine AS runner

RUN apk add --no-cache gettext

ARG PORT=8084
ARG BACKEND_URL=http://backend:8082
ARG FILES_URL=http://files:9000

ENV PORT=${PORT}
ENV BACKEND_URL=${BACKEND_URL}
ENV FILES_URL=${FILES_URL}

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

EXPOSE 8084

CMD ["/bin/sh", "-c", "envsubst '${PORT} ${BACKEND_URL} ${FILES_URL}' < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf && exec nginx -g 'daemon off;'"]
