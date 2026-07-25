FROM ubuntu:24.04@sha256:52df9b1ee71626e0088f7d400d5c6b5f7bb916f8f0c82b474289a4ece6cf3faf

ARG SHERPA_VERSION=v1.13.4
ARG SHERPA_ARCHIVE=sherpa-onnx-v1.13.4-cuda-12.x-cudnn-9.x-linux-x64-gpu.tar.bz2
ARG SHERPA_SHA256=2ba80dd4df761b8de58d578190846f6f2349523685e33bcb24f65ba586c43563

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl bzip2 libgomp1 \
    && curl -fL --retry 3 \
        -o /tmp/sherpa.tar.bz2 \
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/${SHERPA_VERSION}/${SHERPA_ARCHIVE}" \
    && echo "${SHERPA_SHA256}  /tmp/sherpa.tar.bz2" | sha256sum -c - \
    && mkdir -p /opt/sherpa \
    && tar -xjf /tmp/sherpa.tar.bz2 --strip-components=1 -C /opt/sherpa \
    && rm -f /tmp/sherpa.tar.bz2 \
    && apt-get purge -y --auto-remove ca-certificates curl bzip2 \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 6006

ENTRYPOINT ["/opt/sherpa/bin/sherpa-onnx-online-websocket-server"]
