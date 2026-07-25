#!/usr/bin/env bash
set -euo pipefail

readonly model_id="Systran/faster-distil-whisper-large-v3"
readonly api_root="http://127.0.0.1:8000"

/opt/nvidia/nvidia_entrypoint.sh uvicorn --factory speaches.main:create_app &
server_pid=$!

terminate_server() {
  if kill -0 "$server_pid" 2>/dev/null; then
    kill -TERM "$server_pid"
  fi
  wait "$server_pid" || true
}
trap terminate_server TERM INT EXIT

for _attempt in $(seq 1 300); do
  if curl -fsS --max-time 2 "$api_root/health" >/dev/null; then
    break
  fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    wait "$server_pid"
    exit 1
  fi
  sleep 1
done

curl -fsS --max-time 300 -X POST "$api_root/v1/models/$model_id" >/dev/null
curl -fsS --max-time 300 -X POST "$api_root/api/ps/$model_id" >/dev/null

# Loading weights does not initialize every CUDA kernel.  Exercise the real
# transcription endpoint once so the first caller never becomes the warmup.
python - <<'PY'
import wave

with wave.open("/tmp/speaches-warm.wav", "wb") as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(16000)
    output.writeframes(b"\x00\x00" * 8000)
PY
curl -fsS --max-time 300 \
  -F "file=@/tmp/speaches-warm.wav;type=audio/wav" \
  -F "model=$model_id" \
  -F "language=en" \
  -F "response_format=json" \
  "$api_root/v1/audio/transcriptions" >/dev/null

# Health means the API, exact model, and first inference are ready for a caller.
curl -fsS --max-time 5 "$api_root/api/ps" | grep -Fq "$model_id"

wait "$server_pid"
trap - TERM INT EXIT
