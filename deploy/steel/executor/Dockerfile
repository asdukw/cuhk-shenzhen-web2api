FROM node:24-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

COPY server.mjs ./

ENV NODE_ENV=production
EXPOSE 3001

CMD ["node", "server.mjs"]
