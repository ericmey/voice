# voicebook-stream — streaming faster-qwen3-tts service.
#
# Pinned everything. The cu128 restore is load-bearing: faster-qwen3-tts and its
# deps can pull a torch build whose kernels stop before sm_120, silently
# undoing Blackwell support. Install app deps first, force matched cu128
# torch/torchaudio LAST, then assert the wheel identity at build and sm_120 at
# startup. See docs/AGENT-LESSONS.md.
FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
WORKDIR /app

# faster-qwen3-tts pinned to the exact qualified commit (MIT).
ARG FASTER_QWEN_COMMIT=a70afc0f81f7f5f8801c3227968f1102f43f211c
RUN git clone --quiet https://github.com/andimarafioti/faster-qwen3-tts.git /app/faster-qwen3-tts \
    && cd /app/faster-qwen3-tts && git checkout --quiet ${FASTER_QWEN_COMMIT}

COPY pyproject.toml README.md ./
COPY src ./src

# App + service deps, then faster-qwen (torch backend, not the opt-in ggml).
RUN pip install --no-cache-dir . uvicorn \
    && pip install --no-cache-dir /app/faster-qwen3-tts

# LAST, matched cu128 pair — qualified on mizuki 2026-07-22.
RUN pip install --no-cache-dir --force-reinstall \
        torch==2.11.0+cu128 torchaudio==2.11.0+cu128 \
        --index-url https://download.pytorch.org/whl/cu128

# Build-time wheel-identity assertion. get_arch_list() is [] without a GPU
# during build, so the sm_120 KERNEL check runs at STARTUP (StreamingSynthesizer
# __init__). Here we assert the matched cu128 wheels, which is what a clobber
# changes.
RUN python -c "\
import torch, torchaudio, sys; \
print('torch', torch.__version__, '| torchaudio', torchaudio.__version__, '| cuda', torch.version.cuda); \
ok = torch.version.cuda == '12.8' and '+cu128' in torch.__version__ and '+cu128' in torchaudio.__version__; \
sys.exit(0 if ok else 'FATAL: expected matched cu128 wheels')"

EXPOSE 5060
ENV VOICEBOOK_HOST=0.0.0.0
ENTRYPOINT ["python", "-m", "voicebook_stream.server"]
