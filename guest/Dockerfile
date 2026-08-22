FROM node:20-alpine AS builder

WORKDIR /app

COPY package*.json ./
RUN npm ci --include=optional

COPY . .
RUN ln -sfn /app/frontend /frontend
RUN npm run build

FROM nginx:1.31.3-alpine AS runner

RUN apk add --no-cache gettext

ENV PORT=8084
ENV BACKEND_URL=http://202.115.138.57:8082
ENV FILES_URL=http://202.115.138.101:9004

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
RUN find /usr/share/nginx/html -type d -exec chmod 755 {} + \
  && find /usr/share/nginx/html -type f -exec chmod 644 {} +

EXPOSE 8084

CMD ["/bin/sh", "-c", "envsubst '${PORT} ${BACKEND_URL} ${FILES_URL}' < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf && exec nginx -g 'daemon off;'"]
