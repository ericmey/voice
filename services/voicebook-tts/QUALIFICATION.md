# Runtime qualification receipts

Durable record of what was actually installed, loaded, and measured. Chat
summaries and truncated hashes are not the artifact; this file is.

---

## Chatterbox Turbo — 2026-07-22

### Upstream

| field | value |
|---|---|
| repo | `github.com/resemble-ai/chatterbox` |
| commit | `5de7a54aa4e5e2baadb0182dde554908b48b85c2` |
| commit date | 2026-07-21 ("Update ChatterboxNano #542") |
| install | clean source install into a fresh venv |
| class | `chatterbox.tts_turbo.ChatterboxTurboTTS` (source line 111) |
| turbo repo id | `ResembleAI/chatterbox-turbo` |

> **Why source, not pip.** `chatterbox-tts` 0.1.7 from PyPI ships **no
> `tts_turbo.py` at all**. Its `from_pretrained()` loads a hardcoded
> `REPO_ID = "ResembleAI/chatterbox"` and `t3_cfg.safetensors`. An earlier
> comparison downloaded the turbo weights and then never loaded them —
> **downloaded is not loaded.** That entire result was standard Chatterbox and
> was discarded.

### Checkpoint — snapshot `749d1c1a46eb10492095d68fbcf55691ccf137cd`

| file | bytes | sha256 (leading 16) |
|---|---|---|
| `t3_turbo_v1.safetensors` | 1915480052 | `fcf1f8c1d651bb7e` |
| `s3gen.safetensors` | 1056484620 | `2b78103c65420739` |
| `s3gen_meanflow.safetensors` | 1064875036 | `d65cb687a2ed581e` |
| `ve.safetensors` | 5695784 | `f0921cab452fa278` |
| `conds.pt` | 169454 | `b1852099306fd6a7` |

### Environment — captured AFTER install, not before

```
torch       2.11.0+cu128
torchaudio  2.11.0+cu128
cuda rt     12.8
arch list   ['sm_75','sm_80','sm_86','sm_90','sm_100','sm_120']
sm_120      present
real CUDA op (1024x1024 matmul)  finite
```

`pip check` reports the 0.1.7 metadata pin as violated and it runs correctly.
Record as **"works with unsupported dependency versions,"** never "supported."

### Hard constraints found in source

* **`tts_turbo.py:245` — reference length assert.**

  ```python
  assert len(s3gen_ref_wav) / _sr > 5.0, "Audio prompt must be longer than 5 seconds!"
  ```

  Strictly greater than. **A master of exactly 5.0 s also fails.** Sumi's
  11.44 s and Nyla's 9.68 s both clear it. This is a constraint on **master
  authoring**, not just serving — author future masters comfortably above five
  seconds; 10–15 s remains the practical target.

* **`tts_turbo.py:228` — `norm_loudness(wav, sr, target_lufs=-27)`.** Two
  separate facts, deliberately not joined:

  **Established from source:** when enabled, this normalizes the **reference**
  to −27 LUFS inside `prepare_conditionals`. It is intentional upstream policy.

  **Observed separately:** Turbo raw outputs measured −27.86, −26.97 and
  −26.59 LUFS, while the Qwen control measured −21.29 against a master of
  −21.27.

  Those observations are *consistent with* the reference policy. They are not
  proof of it — no output-normalization step was found, so the causal path from
  conditioned reference level to output level is **unverified**. An earlier
  draft of this file said the policy "fully explains" the outputs and that the
  output "inherits that level." Both were causal claims the source alone does
  not support.

### Measured — Sumi master copy, one new neutral sentence

Every parameter pinned to a literal; no library defaults relied on:

```
repetition_penalty=1.2  min_p=0.0  top_p=0.95  exaggeration=0.0
cfg_weight=0.0  temperature=0.8  top_k=1000  norm_loudness=True
```

Seed mechanism (Turbo `generate()` has **no** seed argument) taken from
upstream `gradio_tts_turbo_app.py:70-71`:
`torch.manual_seed` + `torch.cuda.manual_seed` + `torch.cuda.manual_seed_all`.

| seed | generate | output | vs realtime | rms | peak | raw LUFS |
|---|---|---|---|---|---|---|
| 1001 | 2.3 s | 5.36 s | 2.3× faster | 4.17 % | 61.01 % | −27.86 |
| 1002 | 1.4 s | 5.12 s | 3.7× faster | 3.68 % | 33.83 % | −26.97 |
| 1003 | 1.4 s | 4.88 s | 3.5× faster | 4.13 % | 45.98 % | −26.59 |

Observed range **2.3×–3.7× faster than realtime** across three draws. Not an
average, not a rate — three observations.

VRAM: **2.80 GB resident, 3.47 GB peak.**

---

## Qwen Base — control draw, same sentence, 2026-07-22

| field | value |
|---|---|
| model | `Qwen/Qwen3-TTS-12Hz-1.7B-Base` |
| checkpoint | `fd4b254389122332181a7c3db7f27e918eec64e3` |
| generate | 9.7 s for 6.08 s output — **1.6× slower than realtime** |
| VRAM | 4.20 GB resident, 4.79 GB peak |
| raw LUFS | −21.29 (master is −21.27) |

**This is a control draw, not a runtime baseline.** One observation of one
sentence. It does not characterise Qwen Base performance in general.

---

## What none of this establishes

Identity retention (Eric's ear, pending), repeatability beyond the draws
listed, long-form narration, expressive range, paralinguistics, concurrency,
restart behaviour, or serving integration.

---

## Sumi identity gate — 2026-07-22

Blind, R128-matched, four descendants behind a sealed key. Reference was Sumi's
accepted voicebook master; one new neutral sentence; Qwen Base once as control,
Chatterbox Turbo three times on recorded seeds with every parameter pinned to
upstream's own app defaults.

| audition label | runtime | Eric's ear |
|---|---|---|
| candidate_A | **Qwen Base** (control) | **clean direct match** |
| candidate_B | Turbo seed 1002 | close, audible warble — nearest of the three |
| candidate_C | Turbo seed 1003 | audible warble |
| candidate_D | Turbo seed 1001 | audible warble |

### Result

**Qwen Base is the production candidate** for speaking accepted voicebook
masters. It carried Sumi cleanly.

**Chatterbox Turbo is promising but NOT READY — not rejected.** Three draws at
upstream default parameters all carried audible warble; one was close. The
distinction matters: this is a finding about Turbo *as configured*, not about
Turbo as an engine.

### Not a misconfiguration

Checked before reporting, because it was the obvious way to be wrong. The
pinned values match `gradio_tts_turbo_app.py` defaults exactly — temperature
0.8, top_p 0.95, top_k 1000, repetition_penalty 1.2, min_p 0.00, norm_loudness
True. `exaggeration` and `cfg_weight` are not surfaced by that app at all.

### The trade taken

Turbo measured 2.3×–3.7× faster than realtime at 3.47 GB peak. Qwen measured
1.6× *slower* than realtime at 4.79 GB peak. **We keep the slower, heavier
runtime because it is the one that sounds like her** — the same trade taken on
Nyla earlier the same day.

### Open lane

Artifact remediation for Turbo, unblocked from the working system.

**The tuning surface is narrower than it looks.** `tts_turbo.py:290` warns and
**ignores** `cfg_weight`, `exaggeration` **and `min_p`** — all three are inert
for Turbo. (This also confirms pinning them to `0.0` was correct: that is the
value which avoids the warning path.) The parameters that actually do anything
are `temperature`, `top_p`, `top_k`, `repetition_penalty`, `norm_loudness`.

Cheapest untested **hypothesis** — not an established fix: temperature 0.8 with
top_k 1000 is loose sampling and warble is a classic symptom of that, so lower
temperature or lower top_k are the first things to try. On seed 1002, which is
already near the line. Whether either helps is unknown.

The speed difference is an **observed comparison range** from this one
sentence, not a promised win in any future configuration.

### Evidence scope

One master, one sentence, one listener. Three Turbo draws, **one** Qwen draw.
Three-for-three warble is a stronger signal than a single observation; the Qwen
side remains n=1 on this sentence. This is not a general reliability ranking of
either runtime.
