# Dockerfiles that work on pool

Every task runs with no network, a read-only file system and user 65534. So
the image must hold everything, and the program may write only to `/out` and
`/tmp`. The patterns below follow from that.

Patterns marked **tested** come from `examples/run/` in the pool repository
and pass `pool run --local`. The others follow the same rules but were not run.

## Contents

1. General rules
2. Python (tested)
3. Node.js (tested)
4. Java (tested)
5. Go (tested)
6. C and C++ (tested)
7. R (tested)
8. Rust and .NET (not tested)

## 1. General rules

| Do | Why |
|---|---|
| Install every package in a `RUN` line | No network while the task runs. |
| Pin versions (`pillow==11.0.0`, `node:22-alpine`) | The same image every time. |
| Copy data and models into the image (`COPY data/ /data/`) | Jobs cannot read files from the server yet. |
| Use `ENTRYPOINT` for the program and pass options as arguments | `pool run -- --epochs 20` then works like `docker run IMAGE --epochs 20`. |
| Give the program defaults for every option | Then plain `pool run` works. |
| Write results to `/out`, one JSON object to `/out/result.json` | `/out` comes back; `result.json` becomes a table row. |
| Point caches at `/tmp`: `ENV HOME=/tmp` or the tool's cache setting | The home folder is not writable. |
| Make files readable by everyone: `RUN chmod -R a+rX /app` | The program runs as user 65534, not as the files' owner. |
| Build in one stage, run in a small one, for compiled languages | Smaller images download faster to each computer. |

## 2. Python (tested)

```dockerfile
FROM python:3.12-slim
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY train.py /app/train.py
ENTRYPOINT ["python", "/app/train.py"]
```

- scikit-learn, NumPy and SciPy work on the read-only file system as they are.
- pytest needs `-p no:cacheprovider`. Write its report with `--junitxml=/out/junit.xml`.
- Datasets that libraries download on first use must be downloaded in a `RUN` line instead.

## 3. Node.js (tested)

```dockerfile
FROM node:22-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .
ENTRYPOINT ["node", "/app/report.js"]
```

- Node's built-in test runner writes JUnit XML: `node --test --test-reporter=junit --test-reporter-destination=/out/junit.xml test/*.test.js`. Node 22 needs the files listed (a glob), not a folder.
- Split test files over parts with `--test-shard=$((POOL_TASK_INDEX + 1))/$POOL_TASK_COUNT`.
- Size the heap from the memory limit: `node --max-old-space-size=MB`.

## 4. Java (tested)

```dockerfile
FROM eclipse-temurin:21-jdk-alpine AS build
WORKDIR /src
COPY WordCount.java .
RUN javac -d /classes WordCount.java

FROM eclipse-temurin:21-jre-alpine
COPY --from=build /classes /app
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-cp", "/app", "WordCount"]
```

- By default the JVM uses only 25% of the container's memory for its heap. `-XX:MaxRAMPercentage=75` gives it 75%.
- With Maven or Gradle, build the jar in the first stage (dependencies download there, with internet) and copy only the jar.

## 5. Go (tested)

```dockerfile
FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod main.go ./
RUN CGO_ENABLED=0 go build -o /primes .

FROM scratch
COPY --from=build /primes /primes
ENTRYPOINT ["/primes"]
```

- `CGO_ENABLED=0` makes one static binary that runs in an empty (`scratch`) image.
- Go tests: build the test binary in the first stage (`go test -c -o /tests ./...`) and run it. `go test` itself would need to compile at run time.

## 6. C and C++ (tested)

```dockerfile
FROM alpine:3.20 AS build
RUN apk add --no-cache g++ cmake make
WORKDIR /src
COPY CMakeLists.txt heat.cpp ./
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build

FROM scratch
COPY --from=build /src/build/heat /heat
ENTRYPOINT ["/heat"]
```

- For `scratch`, link statically. Otherwise run on the same base as the build (`alpine:3.20`) and install the runtime libraries there.

## 7. R (tested)

```dockerfile
FROM r-base:4.4.2
RUN R -e 'install.packages(c("data.table"), repos = "https://cloud.r-project.org")'
COPY bootstrap.R /app/bootstrap.R
ENTRYPOINT ["Rscript", "/app/bootstrap.R"]
```

- The tested example uses base R only. The `install.packages` line shows where packages go; it was not part of the test.

## 8. Rust and .NET (not tested)

Same idea: build in the SDK image, copy only the result into a small runtime
image.

- **Rust:** `cargo build --release` in a `rust` image, then copy the binary. For `scratch`, build for the `x86_64-unknown-linux-musl` target.
- **.NET:** `dotnet publish -c Release -o /app` in the SDK image, then run `dotnet App.dll` on the runtime image. Set `DOTNET_CLI_HOME=/tmp`, because the home folder is not writable.

Try each one with `pool run --local` before you run it on the group.
