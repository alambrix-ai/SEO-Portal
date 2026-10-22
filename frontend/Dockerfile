# syntax=docker/dockerfile:1

# Local compose uses target `development` (Vite); the compose `production`
# profile uses target `production` (nginx). The free Render deploy path builds
# with `npm run build` on Render's static site instead.

FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .

FROM deps AS development
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

FROM deps AS build
RUN npm run build

# nginx-unprivileged runs as uid 101 (nginx) from the start: no root master,
# no capabilities needed, pid file and temp paths already under /tmp. It
# listens on 10000 here (above 1024, so no NET_BIND_SERVICE either).
FROM nginxinc/nginx-unprivileged:1.27-alpine AS production
# nginx.conf is a template: docker-entrypoint.sh renders it at start (only
# ${API_ORIGIN} and ${CSP_REPORT_URI} are substituted) and writes the
# security-headers snippet for the CSP_REPORT_ONLY mode in force, plus
# /config.js. Everything it writes lands on /tmp through the links below, so
# the container runs with a read-only root filesystem and a tmpfs /tmp;
# nothing under /etc/nginx or the web root needs to be writable. The image's
# files stay root-owned, so the nginx user cannot change them; root is used
# only for these build steps.
USER root
COPY nginx.conf /etc/nginx/console/default.conf.template
COPY docker-entrypoint.sh /docker-entrypoint.sh
COPY --from=build /app/dist /usr/share/nginx/html
# The base image hands /etc/nginx to uid 101; take it back before linking.
RUN chmod 0755 /docker-entrypoint.sh \
    && mkdir -p /etc/nginx/snippets \
    && chown -R root:root /etc/nginx /usr/share/nginx/html \
    && chmod -R go-w /etc/nginx /usr/share/nginx/html \
    && ln -sf /tmp/console-default.conf /etc/nginx/conf.d/default.conf \
    && ln -sf /tmp/console-security-headers.conf /etc/nginx/snippets/security-headers.conf \
    && ln -sf /tmp/console-config.js /usr/share/nginx/html/config.js
# Numeric, so runtimes that require a non-root user can check it.
USER 101
EXPOSE 10000
CMD ["/docker-entrypoint.sh"]
