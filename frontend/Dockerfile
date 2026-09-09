# syntax=docker/dockerfile:1
FROM node:22-alpine AS base
WORKDIR /app

# Dependencies first so a source change does not reinstall them.
COPY package.json ./
RUN npm install --no-audit --no-fund

COPY . .

EXPOSE 5173
# Dev server, so the console is editable in the compose stack. For production
# run `npm run build` and serve `dist/` from a static host or CDN.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
