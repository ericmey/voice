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
| Hana | 0/5 | Hannah x5 | ear review; confident real-name substitution |
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
| E: punctuated text alias `soo, moo, ghee` | conflicting replications | hold; reconcile experimental contract |

Candidate E initially appeared to preserve three units in 5/5 renders, with
the final unit transcribed as the letter name `G`. Two subsequent five-draw
replications through the production registry did not reproduce that result:
the standard `The name is ...` frame recovered no final `G` in 0/5, and the
bare candidate also recovered no final `G` in 0/5. Both complete, unselected
five-draw montages were delivered for auditory comparison. Do not combine these
sets into one rate until voice ID, request path, full text frame, and dictionary
state are proven identical.

Controls established that native `Tsumugi` is spelled letter by letter, native
`Sumugi` collapses toward `SmugMug`, and native `tsunami` is consistently
pronounced as the known word. The constraint is lexical rather than a general
inability to synthesize the `/ts/` onset. No experimental candidate was promoted
to production; the committed dictionary baseline remains live.
