# Build with CMake in a first stage, then keep only the static binary.
FROM alpine:3.20 AS build
RUN apk add --no-cache g++ cmake make
WORKDIR /src
COPY CMakeLists.txt heat.cpp ./
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build

FROM scratch
COPY --from=build /src/build/heat /heat
ENTRYPOINT ["/heat"]
