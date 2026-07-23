# voicebook-stream — streaming faster-qwen3-tts service.
#
# CUDA hygiene by PREVENTION, not cleanup (reviewed 2026-07-22). faster-qwen3-tts
# transitively pulls torch 2.13.0, whose cu13 nvidia runtime packages install
# into the SAME nvidia/<lib>/lib/ paths as the cu12 packages torch 2.11.0+cu128
# needs. A post-hoc `pip uninstall` of a cu13 sibling therefore deletes a .so the
# retained cu12 package still owns — proven: nvidia-cusparselt-cu12 (0.7.1) and
# nvidia-cusparselt-cu13 (0.8.1) both RECORD nvidia/cusparselt/lib/libcusparseLt.so.0
# over one physical file. So we never let the cu13 generation in at all:
#   1. install the matched cu128 torch/torchaudio pair FIRST from the pytorch
#      cu128 index (guarantees the +cu128 wheels), then
#   2. install the app + the pinned faster-qwen commit UNDER a constraints file
#      pinning those exact wheels, so pip cannot resolve torch 2.13.0.
# faster-qwen3-tts requires only torch>=2.5.1, which 2.11.0+cu128 satisfies, so
# the constraint never conflicts. The cu128 wheels use the base image's system
# CUDA 12.8 runtime libs (cublas/cufft/curand/...) and pip-install only the extras
# torch declares (cudnn/cusparselt/nccl/nvshmem/triton, all -cu12).
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

# STEP 1 — matched cu128 pair FIRST, from the explicit cu128 index.
RUN pip install --no-cache-dir \
        torch==2.11.0+cu128 torchaudio==2.11.0+cu128 \
        --index-url https://download.pytorch.org/whl/cu128

# STEP 2 — app + pinned faster-qwen under a constraint that freezes the wheels,
# so neither install can pull torch 2.13.0 (and its cu13 nvidia generation).
RUN printf 'torch==2.11.0+cu128\ntorchaudio==2.11.0+cu128\n' > /etc/cu128-constraints.txt
RUN pip install --no-cache-dir -c /etc/cu128-constraints.txt . uvicorn \
    && pip install --no-cache-dir -c /etc/cu128-constraints.txt /app/faster-qwen3-tts

# Dependency-consistency gate: fails the build if the constraint left any
# requirement unsatisfied or inconsistent.
RUN pip check

# Discriminatory build guard. Passes ONLY when: torch/torchaudio are the matched
# +cu128 builds AND zero non-cu12 (cu13-generation) nvidia distributions remain.
# Prints the full resolved nvidia inventory for the receipt. Red-proofed against
# the preserved polluted image: it exits 1 there, naming all 15 offenders. The
# real-CUDA-op + sm_120 KERNEL check runs at RECEIPT time on the GPU host (no GPU
# at build; torch.cuda.get_arch_list() is [] here) and at STARTUP in
# StreamingSynthesizer.__init__.
RUN python -c "\
import torch, torchaudio, sys; \
from importlib.metadata import distributions; \
nv = sorted((d.metadata['Name'], d.version) for d in distributions() if d.metadata['Name'].lower().startswith('nvidia-')); \
offenders = [f'{n}=={v}' for n, v in nv if not n.lower().endswith('-cu12')]; \
print('torch', torch.__version__, '| torchaudio', torchaudio.__version__, '| cuda', torch.version.cuda); \
print('resolved nvidia inventory (%d):' % len(nv), [f'{n}=={v}' for n, v in nv]); \
print('cu13/non-cu12 offenders:', offenders); \
ok = (torch.version.cuda == '12.8' and '+cu128' in torch.__version__ and '+cu128' in torchaudio.__version__ and not offenders); \
sys.exit(0 if ok else 'FATAL: cu128 wheel mismatch or non-cu12 (cu13) nvidia pollution: ' + repr(offenders))"

EXPOSE 5060
ENV VOICEBOOK_HOST=0.0.0.0
ENTRYPOINT ["python", "-m", "voicebook_stream.server"]
