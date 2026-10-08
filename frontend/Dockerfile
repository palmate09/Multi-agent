# ---- build stage -------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# Install dependencies from the lockfile first so this layer caches across
# source-only changes.
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build

# ---- runtime stage -----------------------------------------------------
# nginx-unprivileged listens on 8080 and runs as uid 101, so no root, no
# capability juggling, and it works with a read-only root filesystem.
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]