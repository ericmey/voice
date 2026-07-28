# parakeet-openai — OpenAI-shaped transcription in front of Riva/Parakeet gRPC.
#
# No model, no GPU, no CUDA. This holds nothing but the seam between the
# OpenAI multipart shape every harness speaks and the streaming gRPC contract
# our `mode=str` Parakeet NIM actually serves. It is deliberately small enough
# to restart instantly and to be obviously not the thing that broke.
#
# ffmpeg is a HARD dependency, not an optional nicety: uploads arrive at
# whatever rate the caller's mic or TTS produced, and Riva needs 16 kHz mono
# s16le. Handing it anything else yields fluent, confident, wrong text.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && pip check

# Build guard: the two things whose absence would only show up under live
# traffic — the Riva client and the ffmpeg binary — are proven present here.
RUN python -c "\
import riva.client, shutil, sys; \
ff = shutil.which('ffmpeg'); \
print('riva.client ok | ffmpeg:', ff); \
sys.exit(0 if ff else 'FATAL: ffmpeg missing; non-16k uploads would be unservable')"

EXPOSE 5057
ENV PARAKEET_OPENAI_HOST=0.0.0.0 \
    PARAKEET_OPENAI_PORT=5057 \
    PARAKEET_URI=parakeet-ctl:50051

# Readiness is measured against the BACKEND, not against this process being up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5057/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "parakeet_openai.server"]
