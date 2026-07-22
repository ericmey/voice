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

RUN pip install --no-cache-dir . qwen-tts soundfile numpy

# LAST, and deliberately after qwen-tts: restore the matched cu128 pair that
# qwen-tts's own resolution would otherwise leave mismatched.
RUN pip install --no-cache-dir --force-reinstall \
        torch torchaudio --index-url https://download.pytorch.org/whl/cu128

# Build-time assertion. If a future dependency bump clobbers cu128 again, the
# IMAGE fails to build rather than the service failing on Nyla's first message.
RUN python -c "\
import torch, torchaudio, sys; \
print('torch', torch.__version__, 'torchaudio', torchaudio.__version__); \
arch = torch.cuda.get_arch_list(); \
print('arch', arch); \
sys.exit(0 if 'sm_120' in arch else 'FATAL: sm_120 missing — cu128 was clobbered')"

EXPOSE 5055
ENV VOICEBOOK_HOST=0.0.0.0
ENTRYPOINT ["python", "-m", "voicebook_tts.server"]
