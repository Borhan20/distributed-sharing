# A Node.js program with its data inside the image: jobs have no network while they run.
FROM node:22-alpine
COPY report.js sales.json /app/
ENTRYPOINT ["node", "/app/report.js"]
