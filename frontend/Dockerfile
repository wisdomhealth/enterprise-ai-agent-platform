FROM node:22-slim AS base

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM base AS test
COPY . .
CMD ["npm", "test", "--", "--run"]

FROM base AS runtime
COPY . .
RUN npm run build
EXPOSE 3000
CMD ["npm", "run", "start"]
