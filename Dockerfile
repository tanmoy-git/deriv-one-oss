# Frontend static assets, served by nginx with /api reverse-proxied to the backend.
#
# Build from the repository root:
#   docker build -t platform-frontend .

FROM node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS builder

WORKDIR /src

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
ENV NODE_ENV=production

# Development convenience only: a pre-issued bearer token for when the UI is not
# running behind an identity proxy. Baked into the built assets, so never set
# this on a build you intend to deploy.
ARG VITE_AUTH_TOKEN=""
ENV VITE_AUTH_TOKEN=${VITE_AUTH_TOKEN}

# Base URL Slack should send events to. Leave unset to use the browser's origin;
# set it when a relay sits in front of this deployment.
ARG VITE_SLACK_GATEWAY_BASE=""
ENV VITE_SLACK_GATEWAY_BASE=${VITE_SLACK_GATEWAY_BASE}

RUN npm run build

FROM nginx:alpine@sha256:a3554c0d0b48177b1413c26e01f695334428020d9df01243fbae3e11cfd17417

COPY docker/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /src/dist /usr/share/nginx/html

EXPOSE 80
