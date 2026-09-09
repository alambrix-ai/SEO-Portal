# syntax=docker/dockerfile:1

# Local compose uses target `development` (Vite). Render/static build uses the
# production image only if you Dockerize the console; the free deploy path
# builds with `npm run build` on Render's static site instead.

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

FROM nginx:1.27-alpine AS production
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.sh /docker-entrypoint.sh
COPY --from=build /app/dist /usr/share/nginx/html
RUN chmod +x /docker-entrypoint.sh
EXPOSE 10000
CMD ["/docker-entrypoint.sh"]
