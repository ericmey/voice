#!/usr/bin/env python3
"""Structured assertions over `docker compose config --format json` for the
Slice-1 stream service. Grades against the FAILURE: a 0.0.0.0 exposure or a
writable mount MUST fail here (red-proofed in test-stream-compose.sh)."""
import json
import sys

DIGEST = "voicebook-stream@sha256:3b28aa8102d69b3214687a7e732dcdeca35b8a11ab0d34187e1dad3f9b4472f7"

d = json.load(open(sys.argv[1]))
fails = []


def ck(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


# canonical project name pinned in the artifact (cwd-independent lifecycle authority);
# a staging-dir-derived or wrong/absent name must fail here.
ck(d.get("name") == "voicebook-stream", "project name pinned == voicebook-stream (cwd-independent)")

svc = (d.get("services") or {}).get("voicebook-stream")
ck(svc is not None, "service voicebook-stream exists")
svc = svc or {}
ck(svc.get("image") == DIGEST, "image == immutable digest sha256:3b28aa8102d6")
ck(svc.get("pull_policy") == "never", "pull_policy never")
ck(svc.get("restart") == "unless-stopped", "restart unless-stopped")

ports = svc.get("ports") or []
ck(len(ports) == 1, "exactly one published port")
p = ports[0] if ports else {}
ck(p.get("host_ip") == "127.0.0.1", "host_ip == 127.0.0.1 (loopback, NOT 0.0.0.0)")
ck(str(p.get("published")) == "5056", "published == 5056")
ck(p.get("target") == 5060, "target == 5060")
ck(p.get("protocol") == "tcp", "protocol tcp")

want = {
    "/srv/voicebook": ("bind", "/srv/voicebook"),
    "/etc/voicebook/registry.json": ("bind", "/srv/voicebook/registry.json"),
    "/models/hf-cache": ("volume", "voicebook-hf-cache"),
}
mounts = {m.get("target"): m for m in (svc.get("volumes") or [])}
ck(len(svc.get("volumes") or []) == 3, "exactly 3 mounts")
for tgt, (typ, src) in want.items():
    m = mounts.get(tgt, {})
    ck(m.get("read_only") is True, f"mount {tgt} read_only (NOT writable)")
    ck(m.get("type") == typ, f"mount {tgt} type {typ}")
    ck(m.get("source") == src, f"mount {tgt} source {src}")

devs = ((svc.get("deploy") or {}).get("resources") or {}).get("reservations", {}).get("devices") or []
ck(any(dev.get("driver") == "nvidia" and "gpu" in (dev.get("capabilities") or []) for dev in devs),
   "nvidia GPU reservation")

env = svc.get("environment") or {}
if isinstance(env, list):
    env = dict(e.split("=", 1) for e in env if "=" in e)
ck(str(env.get("HF_HUB_OFFLINE")) == "1", "HF_HUB_OFFLINE=1 (offline)")
ck(str(env.get("VOICEBOOK_PORT")) == "5060", "VOICEBOOK_PORT=5060")
ck(env.get("VOICEBOOK_HOST") == "0.0.0.0", "VOICEBOOK_HOST=0.0.0.0 (container-internal bind)")
ck(env.get("VOICEBOOK_REGISTRY") == "/etc/voicebook/registry.json", "VOICEBOOK_REGISTRY path")
ck("snapshots/fd4b254389122332181a7c3db7f27e918eec64e3" in str(env.get("VOICEBOOK_MODEL", "")),
   "VOICEBOOK_MODEL pinned snapshot path")
# env keys allowlisted — any unexpected key (e.g. an injected secret) fails
ALLOWED_ENV = {"VOICEBOOK_REGISTRY", "VOICEBOOK_MODEL", "HF_HUB_OFFLINE", "HF_HOME",
               "VOICEBOOK_HOST", "VOICEBOOK_PORT"}
extra_env = sorted(set(env) - ALLOWED_ENV)
ck(not extra_env, f"env keys allowlisted (unexpected rejected: {extra_env})")
blob = json.dumps(env).lower()
ck(not any(s in blob for s in ("api_key", "secret", "gemini", "openai", "elevenlabs", "password", "momo_api", "sk-")),
   "no secret-shaped env values")
# the secrets: section (service or top-level) must be ABSENT — red-proofed
ck(not svc.get("secrets"), "service has NO secrets: block")
ck(not d.get("secrets"), "top-level config has NO secrets: section")

hc = " ".join((svc.get("healthcheck") or {}).get("test") or [])
ck("urllib.request.urlopen" in hc and "/healthz" in hc, "python-stdlib healthcheck -> /healthz")

nets = d.get("networks") or {}
vd = nets.get("voice_default") or {}
ck(vd.get("external") is True and vd.get("name") == "voice_default",
   "network voice_default external + name voice_default")
vols = d.get("volumes") or {}
hv = vols.get("voicebook-hf-cache") or {}
ck(hv.get("external") is True and hv.get("name") == "voicebook-hf-cache",
   "volume voicebook-hf-cache external + name voicebook-hf-cache")

print("== STREAM_COMPOSE_TEST=" + ("PASS" if not fails else "FAIL") + " ==")
sys.exit(0 if not fails else 1)
