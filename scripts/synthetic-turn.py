"""Slice 7 — single-client synthetic turn against the isolated Sumi worker.

Dispatch phone-sumi into a fresh room, join as a synthetic caller ("Eric"),
speak one utterance (synthesized via voicebook nyla-v1 so it's self-contained),
and capture Sumi's audio back. Proves the whole local loop in one shot:
  caller speech -> Parakeet STT -> Momo LLM -> voicebook TTS -> Sumi audio.

Emits latency marks and writes Sumi's captured audio to a WAV. The transcript
JSON (who-said-what + ASR/LLM marks) is read separately from logs/voice after.
No SIP, no DID, no PSTN — a LiveKit client is the caller.
"""

import asyncio
import json
import os
import time
import urllib.request
import wave

from livekit import api, rtc

LK_WS = os.environ.get("LK_WS", "ws://livekit-server:7880")
LK_HTTP = os.environ.get("LK_HTTP", "http://livekit-server:7880")
API_KEY = os.environ["LIVEKIT_API_KEY"]
API_SECRET = os.environ["LIVEKIT_API_SECRET"]
VB = os.environ.get("VB", "http://voicebook-stream:5060")
ROOM = os.environ.get("ROOM", "sumi-synthetic-1")
CALLER_TEXT = os.environ.get(
    "CALLER_TEXT",
    "Hello Sumi. It is good to finally hear your voice. Can you tell me a little about who you are?",
)
OUT = os.environ.get("OUT", "/app/logs/voice/sumi_synthetic_capture.wav")
SR = 24000


def _fetch_caller_pcm() -> bytes:
    body = json.dumps({"voice_id": "nyla-v1", "text": CALLER_TEXT}).encode()
    req = urllib.request.Request(
        f"{VB}/speak/stream", data=body, headers={"Content-Type": "application/json"}
    )
    return urllib.request.urlopen(req, timeout=40).read()  # raw s16le 24k mono


async def main() -> None:
    t0 = time.time()
    lkapi = api.LiveKitAPI(LK_HTTP, API_KEY, API_SECRET)
    await lkapi.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(agent_name="phone-sumi", room=ROOM)
    )
    print(f"[{time.time()-t0:5.2f}s] dispatched phone-sumi -> room {ROOM}", flush=True)

    token = (
        api.AccessToken(API_KEY, API_SECRET)
        .with_identity("eric-caller")
        .with_name("Eric")
        .with_grants(api.VideoGrants(room_join=True, room=ROOM))
        .to_jwt()
    )

    room = rtc.Room()
    agent_frames: list[bytes] = []
    marks = {"first_audio": None, "sr": None, "ch": None}

    @room.on("track_subscribed")
    def _on_sub(track, pub, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            print(f"[{time.time()-t0:5.2f}s] subscribed to {participant.identity} audio", flush=True)

            async def _read():
                async for ev in rtc.AudioStream(track):
                    f = ev.frame
                    if marks["first_audio"] is None:
                        marks["first_audio"] = time.time() - t0
                        marks["sr"], marks["ch"] = f.sample_rate, f.num_channels
                    agent_frames.append(bytes(f.data))

            asyncio.create_task(_read())

    await room.connect(LK_WS, token)
    print(f"[{time.time()-t0:5.2f}s] caller connected", flush=True)

    caller_pcm = await asyncio.get_event_loop().run_in_executor(None, _fetch_caller_pcm)
    print(f"[{time.time()-t0:5.2f}s] caller speech ready ({len(caller_pcm)/2/SR:.2f}s)", flush=True)

    # let Sumi join + greet
    await asyncio.sleep(7)
    greet_ttfa = marks["first_audio"]
    print(f"[{time.time()-t0:5.2f}s] greeting TTFA={greet_ttfa}", flush=True)
    frames_before = len(agent_frames)

    # publish the caller utterance
    source = rtc.AudioSource(SR, 1)
    track = rtc.LocalAudioTrack.create_audio_track("caller", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    spf = SR // 50  # 20ms frames
    fb = spf * 2
    pub0 = time.time()
    for i in range(0, len(caller_pcm), fb):
        chunk = caller_pcm[i : i + fb]
        if len(chunk) < fb:
            chunk = chunk + b"\x00" * (fb - len(chunk))
        await source.capture_frame(rtc.AudioFrame(chunk, SR, 1, spf))
    print(f"[{time.time()-t0:5.2f}s] caller utterance published (took {time.time()-pub0:.2f}s wall)", flush=True)

    # wait for Sumi's response turn (STT->LLM->TTS), and DON'T cut her off:
    # detect first response audio, then keep capturing well past it so the full
    # spoken reply lands in the WAV before the caller disconnects.
    resp_t0 = time.time()
    first_resp = None
    for _ in range(300):  # up to ~30s
        await asyncio.sleep(0.1)
        if len(agent_frames) > frames_before and first_resp is None:
            first_resp = time.time() - resp_t0
            frames_before = 10**9  # latch first-response mark
    response_started = first_resp is not None

    if agent_frames:
        w = wave.open(OUT, "wb")
        w.setnchannels(marks["ch"] or 1)
        w.setsampwidth(2)
        w.setframerate(marks["sr"] or SR)
        for b in agent_frames:
            w.writeframes(b)
        w.close()
        total_s = sum(len(b) for b in agent_frames) / 2 / (marks["sr"] or SR)
    else:
        total_s = 0.0

    print("=" * 60, flush=True)
    print(f"RESULT greeting_ttfa={greet_ttfa}", flush=True)
    print(f"RESULT response_after_utterance={'YES' if response_started else 'NO'} first_response_latency={first_resp}", flush=True)
    print(f"RESULT total_sumi_audio={total_s:.2f}s frames={len(agent_frames)} rate={marks['sr']} ch={marks['ch']}", flush=True)
    print(f"RESULT capture -> {OUT}", flush=True)

    await room.disconnect()
    await lkapi.aclose()


asyncio.run(main())
