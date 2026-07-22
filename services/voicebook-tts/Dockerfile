# Voicebook TTS — persistent Qwen Base service.
#
# The cu128 pin below is load-bearing, not cosmetic. On 2026-07-22 three
# separate packages (Orpheus-FastAPI, qwen-tts, chatterbox-tts) each pulled or
# pinned a torch build whose kernel set stops before sm_120, silently undoing a
# verified cu128 environment on Blackwell. The failure is quiet: the container
# builds, starts, answers, and never touches the card.
#
# So: install the app deps FIRST, then force the matched cu128 pair LAST, then
# assert sm_120 at build time. Order matters.
FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

# Pinned to the exact versions qualified on mizuki 2026-07-22. Install order
# protects sm_120 today; pinning is what keeps tomorrow reproducible.
RUN pip install --no-cache-dir . \
        qwen-tts==0.1.1 \
        soundfile==0.14.0 \
        numpy==2.4.6

# LAST, and deliberately after qwen-tts: restore the matched cu128 pair that
# qwen-tts's own resolution would otherwise leave mismatched.
RUN pip install --no-cache-dir --force-reinstall \
        torch==2.11.0+cu128 torchaudio==2.11.0+cu128 \
        --index-url https://download.pytorch.org/whl/cu128

# Build-time assertion — what is ACTUALLY checkable without a GPU.
#
# An earlier version asserted sm_120 was in torch.cuda.get_arch_list() here.
# That can never pass: `docker build` has no GPU, CUDA never initialises, and
# get_arch_list() returns []. The gate was not strict, it was impossible — it
# would have failed every build on every machine forever.
#
# What IS verifiable at build time is which wheel got installed, which is the
# thing the clobber defect actually changes.
RUN python -c "\
import torch, torchaudio, sys; \
print('torch', torch.__version__, '| torchaudio', torchaudio.__version__, '| cuda', torch.version.cuda); \
ok = torch.version.cuda == '12.8' and '+cu128' in torch.__version__ and '+cu128' in torchaudio.__version__; \
sys.exit(0 if ok else 'FATAL: expected matched cu128 builds, got torch %s / torchaudio %s / cuda %s' % (torch.__version__, torchaudio.__version__, torch.version.cuda))"

# The sm_120 kernel assertion runs at STARTUP, in QwenBaseSynthesizer.__init__,
# where the nvidia runtime has actually attached a device. That is the only
# place the question can be answered.

EXPOSE 5055
ENV VOICEBOOK_HOST=0.0.0.0
ENTRYPOINT ["python", "-m", "voicebook_tts.server"]
