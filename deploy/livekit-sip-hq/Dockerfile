ARG GOVERSION=1.26

FROM golang:${GOVERSION} AS builder

ARG TARGETARCH
ARG SIP_COMMIT=d35b96aa5f550de51e284b5fe46b1373f39a6725
ARG MEDIA_SDK_COMMIT=966281e07f53f3ef28c30a534c529e58d6fbf152
ARG VERSION=1.8.0-hq.1

RUN apt-get update && apt-get install -y --no-install-recommends \
      git pkg-config libopus-dev libopusfile-dev libsoxr-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
RUN git clone https://github.com/livekit/sip.git sip && \
    git -C sip checkout "${SIP_COMMIT}" && \
    git clone https://github.com/livekit/media-sdk.git media-sdk && \
    git -C media-sdk checkout "${MEDIA_SDK_COMMIT}"

# Telephony output is 8 kHz PCMU. Use libsoxr's high-quality anti-aliasing
# profile for LiveKit's 48 kHz room audio -> 8 kHz SIP conversion.
RUN sed -i 's/C\.SOXR_LQ/C.SOXR_HQ/' media-sdk/resample_soxr.go && \
    grep -q 'C.SOXR_HQ' media-sdk/resample_soxr.go && \
    ! grep -q 'C.SOXR_LQ' media-sdk/resample_soxr.go && \
    cd sip && go mod edit -replace github.com/livekit/media-sdk=/workspace/media-sdk && \
    go mod download

WORKDIR /workspace/sip
RUN CGO_ENABLED=1 GOOS=linux GOARCH="${TARGETARCH}" GO111MODULE=on go build -a \
    -ldflags "-X github.com/livekit/sip/version.Version=${VERSION}" \
    -o /workspace/livekit-sip ./cmd/livekit-sip

FROM debian:trixie-slim
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      libopus0 libopusfile0 libsoxr0 ca-certificates wget && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
COPY --from=builder /workspace/livekit-sip /bin/livekit-sip
WORKDIR /sip
ENTRYPOINT ["livekit-sip", "--config=/sip/config.yaml"]
