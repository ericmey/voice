# LiveKit SIP HQ resampler build

This image is LiveKit SIP v1.8.0 with one intentional dependency patch:
`github.com/livekit/media-sdk/resample_soxr.go` uses `SOXR_HQ` instead of
`SOXR_LQ` for the 48 kHz room-audio to 8 kHz telephony conversion.

Both upstream repositories are pinned to the exact commits used by the stock
v1.8.0 build. The Docker build verifies that the replacement occurred before
compiling. The stock `livekit/sip:v1.8.0` image remains the rollback.

Build:

```sh
docker build -t livekit/sip:v1.8.0-soxr-hq.1 .
```
