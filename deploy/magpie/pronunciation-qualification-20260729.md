# Magpie pronunciation qualification — 2026-07-29

## Method

Each production dictionary entry was rendered five independent times with
`yua-v1`, quality 40, and the frame `The name is <name>.` through the production
registry. Every WAV was then transcribed independently by the fleet Parakeet
service. This is an ASR failure-detector pass, not auditory acceptance: proper
names can sound correct to a human while still being missed by Parakeet. Eric's
ear remains authoritative.

The result supersedes the earlier single-render triage. Magpie is stochastic;
qualification is based on the five-draw distribution and never on the best
sample.

## Results

| Entry | Exact ASR recovery | Five outputs | Assessment |
| --- | ---: | --- | --- |
| Aoi | 0/5 | we; Ai; a; empty; Ai | ear review |
| Hana | unresolved | Hannah x5 | ASR cannot distinguish Hana from Hannah |
| Katy | 5/5 | Katie x5 | stable detector pass |
| Mia | 5/5 | Mia x5 | stable detector pass |
| Mizuki | 0/5 | Suzuki x3; Miki x2 | ear review |
| Momo | 5/5 | Momo x5 | stable detector pass |
| Nana | 5/5 | Nana x5 | stable detector pass |
| Nyla | 0/5 | Nya; Kyla x2; Nile; nylon | ear review |
| Rika | 5/5 | Rika x5 | stable detector pass |
| Shiori | 0/5 | your; short x2; sure; Chi | highest-priority ear review |
| Sumi | 3/5 | Sumi x3; Sum; empty | mostly stable; ear review |
| Tama | 4/5 | Tama x4; Tom | mostly stable; ear review |
| Vesper | 5/5 | Vesper x5 | stable detector pass |
| Yua | 0/5 | empty x3; Wa; Ua | ear review |
| Musubi | 0/5 | Musi x4; Muzo | ear review |
| Tsumugi | 0/5 | Sum x2; Sumo; Sui; Sue | fails detector pass |

## Tsumugi candidate experiment

The initial conclusion that a terminal token had been truncated was disproven
by placing `Tsumugi` twice in mid-sentence with a complete trailing clause. The
token itself degrades; later audio remains intact.

Five-draw candidate results:

| Candidate | ASR distribution | Result |
| --- | --- | --- |
| A: compact IPA, medial stress | Smug/SmugMug 5/5 | reject |
| B: spaced phones, medial stress | Smug/SmugMug 5/5 | reject |
| C: spaced phones, initial stress | Tui x4; Tumi x1 | reject |
| D: text alias `soo moo ghee` | exact once; degraded/partial four times | reject for production reliability |
| E: punctuated text alias `soo, moo, ghee` | position-dependent; sentence-final fails | reject for production reliability |

Candidate E initially appeared to preserve three units in 5/5 renders, with
the final unit transcribed as the letter name `G`. The apparent contradiction
was an uncontrolled text-position variable: those draws used a trailing clause,
while the first reproduction ended immediately after the candidate. Controlled
reruns converged: sentence-final recovered the final `G` in 1/5 draws in one
set and 0/5 in another; mid-sentence recovered it in 2/3 follow-up draws. The
bare candidate also recovered no final `G` in 0/5. Both complete, unselected
five-draw montages were delivered for auditory comparison. Candidate E fails
the sentence-final production gate even though a trailing clause can preserve
its terminal syllable.

This establishes text position as a required qualification variable. Keep the
full frame identical within a sample set, test both terminal and mid-sentence
conditions, label every reported rate with its frame, and accept on the weaker
sentence-final distribution rather than pooling positions.

Controls established that native `Tsumugi` is spelled letter by letter, native
`Sumugi` collapses toward `SmugMug`, and native `tsunami` is consistently
pronounced as the known word. The constraint is lexical rather than a general
inability to synthesize the `/ts/` onset. No experimental candidate was promoted
to production; the committed dictionary baseline remains live.

## Musubi follow-up

Musubi was evaluated separately because its baseline failure was stable rather
than stochastic. Every candidate used five sentence-final and five mid-sentence
draws unless noted.

| Condition | ASR distribution | Result |
| --- | --- | --- |
| Production dictionary baseline | Musi x9; Mizi x1 | fails both positions |
| Native G2P, Musubi override absent | Musi x10 | override is not the cause |
| Spaced IPA `m u s u b i` | Musi x8; Muzo x1; Mizi x1 | no improvement |
| Final-unit stress `m u s u ˈ b i` | Musi x10 | reject phoneme lane |
| Text alias `moo soo bee` | sentence-final preserves all three ASR units 1/5 | auditory review only |
| Punctuated text alias `moo, soo, bee` | sentence-final loses final unit 5/5 | reject |

The phoneme layer cannot reliably preserve Musubi's final `bi` unit. Five
unselected sentence-final draws of the plain text alias were delivered as one
auditory montage. No Musubi experiment was promoted. If the montage is not
consistently acceptable to Eric, use the semantic speech alias “the memory
system” rather than continuing phoneme tuning.

## Final-CV shape hypothesis

Tsumugi (`tsu-mu-gi`), Musubi (`mu-su-bi`), Mizuki (`mi-zu-ki`), and Shiori
(`shi-o-ri`) all have three syllables ending in a light consonant-vowel unit.
Mizuki and Shiori were tested at five draws in both positions to determine
whether all four shared one terminal-unit failure class.

| Name | Sentence-final | Mid-sentence | Finding |
| --- | --- | --- | --- |
| Mizuki | Miki x4; Suzuki x1 | Suzuki x4; Miki x1 | final `ki` preserved 10/10; earlier substitution |
| Shiori | so; Shri x2; shy; Shore | Si x3; Sri; so | whole-name compression, not stable final-unit deletion |

The shared-shape hypothesis is refuted. Position changes the attractor
distribution, but Mizuki preserves its final unit in every draw and Shiori's
compression does not match Musubi's deterministic dropped-`bi` behavior. Treat
these as distinct pronunciation defects rather than applying one class-wide
alias or tuning strategy.

## Lexical-attraction confound

Many ASR outputs are nearby high-frequency words or real names: `Hannah`,
`Miki`, `Suzuki`, `Shore`, and `SmugMug`. This suggests lexical attraction, but
does not establish which model supplies it. Parakeet is itself a lexical decoder
and can map an acoustically reasonable unfamiliar name to a familiar token.

Riva's gRPC response schema exposes experimental `meta.processed_text`, which
could separate Magpie preprocessing from downstream ASR. A live native Magpie
probe returned seven streaming chunks with the metadata field empty in every
chunk, so that discriminator is unavailable in this deployment. Treat lexical
attraction as a hypothesis, not a generator finding. Do not derive a class-wide
pronunciation rule from ASR vocabulary alone; retain per-name controlled draws
and Eric's auditory acceptance.

### Hana discrimination control

Four sentence-final renders each of written `Hana` and written `Hannah` all
decoded as `Hannah`. Parakeet therefore cannot discriminate the target HAH-nah
from the near-miss HAN-uh under this test, and the earlier claim that Magpie was
rendering Hana as Hannah is withdrawn. Hana remains auditorily unresolved.

Before trusting ASR to separate any two pronunciation candidates, first test
whether it can distinguish human-verified reference audio for them. Identical
decodes invalidate the discriminator. Different decodes are necessary but not
sufficient evidence: a same-TTS spelling control can still contain synthesis
error and cannot create acoustic ground truth. Gross corruption remains useful
for triage, but no ASR transcript replaces human auditory acceptance.
