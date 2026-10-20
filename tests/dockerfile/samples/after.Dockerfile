# syntax=docker/dockerfile:1
# Build the server in one stage and ship it in a slim one.

ARG GO_VERSION=1.22

# Compile the binary with the project's build script.
FROM golang:${GO_VERSION} AS builder
WORKDIR /src
COPY go.mod go.sum ./
COPY scripts/ ./scripts/
ENV CGO_ENABLED=0 \
    GOFLAGS=-trimpath
RUN --mount=type=cache,target=/root/.cache \
    ./scripts/build.sh --out /out/server

FROM builder AS unit-test
RUN bash scripts/test.sh --race

# The runtime image.
FROM debian:bookworm-slim
COPY --from=builder /out/server /usr/local/bin/server
COPY docker-entrypoint.sh /usr/local/bin/
COPY --chmod=0755 files/tools/* /usr/local/bin/
ENV PATH=/opt/tools:$PATH
RUN install-deps --minimal
EXPOSE 8443
ENTRYPOINT ["docker-entrypoint.sh"]
HEALTHCHECK CMD ["healthcheck"]
CMD ["server"]
