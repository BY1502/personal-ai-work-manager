FROM node:22-bookworm-slim AS build

WORKDIR /app

RUN corepack enable && corepack prepare pnpm@11.16.0 --activate

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .

ARG NEXT_PUBLIC_API_BASE_URL=
ARG NEXT_PUBLIC_SITE_URL=http://localhost:3100
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL} \
    NEXT_PUBLIC_SITE_URL=${NEXT_PUBLIC_SITE_URL} \
    WRANGLER_LOG_PATH=/tmp/wrangler-build.log

RUN pnpm run build

FROM node:22-bookworm-slim

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000 \
    WRANGLER_LOG_PATH=/tmp/wrangler-runtime.log

WORKDIR /app

RUN corepack enable && corepack prepare pnpm@11.16.0 --activate

COPY --from=build /app ./

EXPOSE 3000

CMD ["pnpm", "exec", "vinext", "start", "--hostname", "0.0.0.0", "--port", "3000"]
