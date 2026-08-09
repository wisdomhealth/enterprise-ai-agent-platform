FROM node:22-slim AS deps

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM deps AS test
COPY app ./app
COPY tests ./tests
COPY *.config.* next-env.d.ts test-setup.ts tsconfig.json ./
CMD ["npm", "test", "--", "--run"]

FROM deps AS builder
COPY app ./app
COPY eslint.config.mjs next-env.d.ts next.config.ts postcss.config.mjs tsconfig.json ./
RUN npm run build

FROM node:22-slim AS runner

ENV NODE_ENV=production \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
RUN chown node:node /app
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
