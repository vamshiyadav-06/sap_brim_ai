
# Web image: builds the React app and serves it with Caddy, which also terminates HTTPS (automatic
# Let's Encrypt certificates) and forwards /api and /health to the backend container.
# Used by docker-compose.prod.yml (see DEPLOYMENT.md). Build context: frontend/.

FROM node:22.23.4-bookworm-slim AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# Empty (default): the app calls the same-origin path /api, which Caddy forwards to the backend.
# Set it only when the API is served from another origin, e.g. https://api.example.com/api.
ARG VITE_API_BASE_URL=
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
ARG VITE_REGISTRATION_ENABLED=false
ENV VITE_REGISTRATION_ENABLED=${VITE_REGISTRATION_ENABLED}
RUN npm run build

FROM caddy:2.11.7-alpine
COPY Caddyfile /etc/caddy/Caddyfile
# Access snippets selected by ACCESS_MODE (open | basic_auth); see caddy/access/.
COPY caddy/access /etc/caddy/access
COPY --from=build /src/dist /srv
