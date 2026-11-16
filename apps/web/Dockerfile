# syntax=docker/dockerfile:1.7
# Next.js standalone image. Build context is the repository root because the web
# app depends on the workspace package @clipforge/shared.

FROM node:22.14.0-bookworm-slim AS base
ENV NEXT_TELEMETRY_DISABLED=1
# Corepack's pnpm shim expects bin/pnpm.cjs, which pnpm 12 no longer ships. Install
# the pinned CLI with npm instead so the image uses the same version as the repo.
RUN npm install --global pnpm@12.11.2
WORKDIR /repo

FROM base AS deps
# Only manifests first: dependency layers survive application edits.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/package.json
COPY packages/shared/package.json packages/shared/package.json
RUN --mount=type=cache,id=pnpm-store,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile

FROM deps AS builder
ENV NEXT_OUTPUT=standalone
COPY . .
RUN pnpm --filter @clipforge/web build

FROM node:22.14.0-bookworm-slim AS runner
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN groupadd --system --gid 1001 nodejs \
    && useradd --system --uid 1001 --gid nodejs --no-create-home nextjs

WORKDIR /app
# The tracing root is the monorepo root, so the standalone tree keeps that shape.
COPY --from=builder --chown=nextjs:nodejs /repo/apps/web/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /repo/apps/web/.next/static ./apps/web/.next/static
COPY --from=builder --chown=nextjs:nodejs /repo/apps/web/public ./apps/web/public

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/api/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"

CMD ["node", "apps/web/server.js"]
