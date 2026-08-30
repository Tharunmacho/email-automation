# syntax=docker/dockerfile:1

FROM node:22-alpine AS dependencies

RUN apk add --no-cache libc6-compat
WORKDIR /app

# Keep dependency installation cached until the lockfile changes.
COPY package.json package-lock.json ./
RUN npm ci


FROM node:22-alpine AS builder

WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=dependencies /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC variables are compiled into the browser bundle. This default is
# the deployed FastAPI service; override it when building for another environment.
ARG NEXT_PUBLIC_API_BASE=email-automation-emailbackend-tfykto-dc98c8-147-79-71-40.sslip.io
ENV NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}

RUN npm run build


FROM node:22-alpine AS runner

RUN apk add --no-cache libc6-compat \
    && addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs

WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

# Standalone output contains the production server and only the traced modules
# it needs. Public and static assets are copied separately by Next.js design.
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

CMD ["node", "server.js"]
