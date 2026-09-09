# syntax=docker/dockerfile:1
FROM node:22-alpine AS build
WORKDIR /app

# Dependencies first so a source change does not reinstall them.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build

FROM nginx:1.27-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.sh /docker-entrypoint.sh
COPY --from=build /app/dist /usr/share/nginx/html

RUN chmod +x /docker-entrypoint.sh

EXPOSE 10000

CMD ["/docker-entrypoint.sh"]
