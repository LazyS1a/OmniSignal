FROM golang:1.26.6-alpine3.23@sha256:e57c41c1d5864341031181b0db34b9a537bb5773eb6428e4e5bdaea0f9135406 AS gosu-builder

RUN GOBIN=/out CGO_ENABLED=0 go install github.com/tianon/gosu@1.19 \
    && /out/gosu --version \
    && /out/gosu nobody true

FROM postgres:18.6-alpine3.23@sha256:697c180dbf244d3ce4a8f4cbc0156cde840af055c1bf8b76aebe422a4822086f

COPY --from=gosu-builder /out/gosu /usr/local/bin/gosu

RUN apk upgrade --no-cache \
    && gosu --version \
    && gosu nobody true
