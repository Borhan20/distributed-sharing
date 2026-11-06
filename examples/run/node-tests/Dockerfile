# A test suite with Node's built-in test runner: no packages to install.
FROM node:22-alpine
WORKDIR /app
COPY cart.js run-tests.sh ./
COPY test/ test/
ENTRYPOINT ["sh", "/app/run-tests.sh"]
