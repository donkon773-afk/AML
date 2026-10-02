# AML/NAM Tests, Benchmarks, and Measurements

This document separates automated implementation checks, transport measurements, and local-model generation experiments. Results apply to the listed revisions, dates, hardware, and data; they are not universal guarantees.

## 1. Current offline verification

**Verified on 2026-10-02** on a clean temporary copy of the source `HEAD`, so the tests did not touch the working board or uncommitted files. Command: `python verify.py`, with Python dependencies from `requirements-test.txt` and Node.js dependencies installed from the lockfile.

Recorded run details: [`review/2026-10-02/GITHUB_VERIFY.md`](../review/2026-10-02/GITHUB_VERIFY.md).

| Check | Result |
|---|---|
| Python `unittest` | **150 tests passed**; final status `OK` |
| JavaScript Node test runner | **14/14 passed** |
| GBNF equivalent-language check | **Passed:** valid, invalid, and truncated frames; repetition boundaries |
| Offline demo | **Passed:** valid keyframe/proposal accepted; ghost target and incomplete frame rejected |
| Overall gate | `OFFLINE PASSED`, exit code `0` |

The Python suite covers codecs, grammars, NAM schema, Receiver, regressions, `s:2`, file bus, STATE/DELTA publishing, synchronization, rotation, and the verification gate. `test_ci.py` also exercises retry handling for a command that fails temporarily; the full suite passed on the final run.

The GBNF check uses a parser library and equivalent bounded expansion for repetition limits. **It does not load or validate the native LM Studio/llama.cpp engine.** Offline tests do not verify live server behavior, actual constrained decoding, model decision accuracy, or production deployment.

For reproduction, follow the commands in the root [README](../README.en.md). `verify.py` returns `2` when a check is incomplete (for example, Node.js or the GBNF package is missing); that is not a passing result.

## 2. Live frame generation by local models (2026-09-24)

Report: [`review/2026-09-24/LIVE_FRAMES.md`](../review/2026-09-24/LIVE_FRAMES.md); raw data: `review/2026-09-24/live_frames_raw.json`. **200 requests** were run: 20 for each of five kinds (`HANDOFF`, `RES`, `QUERY`, `ERR`, `PROP`) in each of two configurations. The cluster acceptance threshold counts the first four kinds; `PROP` is a separate profile and reference result.

| Model/configuration | Cluster kinds, strict | Lenient | Receiver accept | Semantic | p95 generation |
|---|---:|---:|---:|---:|---:|
| qwen3.5-9b, SDK + GBNF | 80/80 = **100%** | 100% | 100% | 100% | ≤ 5.45 s |
| gemma-4-12b-coder, HTTP freeform | 72/80 = **90%** | 100% | 100% | 100% | ≤ 12.45 s |

`Strict` means the entire output passes validation without extracting or repairing text. For `gemma` `RES`, strict was **12/20 = 60%**: eight times the model added an explanatory second line after a valid frame. `extract_frame` accepted the first line. This is below the 80% strict target for that message kind and requires an explicitly enabled lenient path or a suitable stop sequence.

For reference `PROP` measurements, qwen scored 17/20 (85%) semantic and gemma 18/20 (90%). `PROP` is excluded from the cluster threshold. These measurements describe formatting, receiver acceptance, and latency on the tested configuration; they do not establish the quality of every decision or portability to other machines/model versions.

## 3. Cluster file-bus latency

The T38 report dated 2026-09-24 recorded **60/60 deliveries** in a copy of `.agent-sync`, with a 0.5-second polling interval:

| Metric | Value | Threshold | Result |
|---|---:|---:|---|
| p50 outbox → log entry | 0.314 s | — | measured |
| p95 outbox → log entry | 0.513 s | ≤ 2 s | passed; about 3.9× below threshold |
| Maximum | 0.566 s | — | measured |

This is a transport measurement in one local environment. It is not a network SLA and excludes model generation.

## 4. Compactness and traffic

### Real `s:2` board snapshot (T37, 2026-09-24)

For a snapshot with 15 tasks and 4 agents:

| Representation | Size |
|---|---|
| AML `s:2` | 1,014 bytes / 496 `cl100k_base` tokens / 496 `o200k_base` tokens |
| NAM JSON | 2,778 bytes / 891 `cl100k_base` tokens / 863 `o200k_base` tokens |
| Difference | −63.5% bytes / −44.3% `cl100k_base` / −42.5% `o200k_base` |

### Overnight STATE/DELTA stream (T38, sample from 2026-09-24)

For 14 frames (1 STATE and 13 DELTA), the stream used 3,786 AML bytes versus 9,899 JSON bytes (**−62% bytes**). Reported token counts were 1,657 versus 2,859 (**−42%**), but tokens were not available for all frames before a restart on the environment with `tiktoken`; this is a partial sample. DELTA median was 139 bytes, with a 452-byte maximum; a 23-row STATE was 1,229 bytes.

The main traffic reduction here comes from **sending DELTA instead of repeating a complete snapshot** and avoiding publication when the board has not changed, not only from shortening field names.

### Reference compact set

Scenario documents report a historical comparison of 919 AML bytes versus 1,812 JSON bytes (−50%). Treat it as a result for that specific example set, not a universal percentage for arbitrary messages. A separate comparison with TOON is recorded in `review/2026-09-15/T16-antigravity-benchmark.txt`.

## 5. What these measurements do not prove

- Universal token/byte savings for arbitrary conversations.
- Correct meaning of every decision proposed by an LLM.
- Protection from sender spoofing: the current file-bus implementation does not sign frames.
- Safety on an untrusted network or multi-machine authorization.
- Latency and quality on a different model, quantization, prompt, backend, or hardware.
- GBNF support in a particular native engine version unless a separate live probe was run.

## 6. Supporting evidence

- Offline gate and criteria: `verify.py`, `evidence/verify.txt`, `review/2026-09-24/T36-verify.txt`.
- Live models: `review/2026-09-24/LIVE_FRAMES.md` and `live_frames_raw.json`.
- Traffic/tokens: `review/2026-09-24/T38-tokens.md`.
- Bus latency: `review/2026-09-24/T38-bus-latency.txt`.
- Release contents: `docs/RELEASE_R2.md`.
