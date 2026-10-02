# AML/NAM

**AML/NAM** is a compact text wire format for structured messages between agents and local software. This repository contains the protocol specification, Python and JavaScript codecs, Lark and GBNF grammars, the NAM/2 schema, a validating receiver, a file-bus prototype, tests, and reproducible reports.

**Languages:** English | [Русский](README.md)

> **Status:** Research prototype and pilot transport. Offline validation and limited local-model measurements are available. The current file bus is for a local environment; multi-machine use requires authenticated peers and a protected transport.

## Start here

- [Protocol specification](docs/PROTOCOL_SPEC.en.md) — framing, message kinds, profiles, validation, and security limits.
- [Cluster messaging guide](docs/CLUSTER_GUIDE.en.md) — examples and the agent workflow.
- [Compatibility notes](docs/COMPATIBILITY.en.md) — NAM dialects and the `s:2` cluster profile.
- [Tests and benchmarks](docs/TESTS_AND_BENCHMARKS.en.md) — methods, dated results, and limits on interpretation.
- [End-to-end exchange scenario](docs/SCENARIO-cluster-exchange.en.md).
- [AML generation prompt](docs/AML_SYSTEM_PROMPT.en.md) — an example prompt to use with a matching grammar and response validation.
- [Contributing](CONTRIBUTING.en.md).

## Repository contents

| Component | Location |
|---|---|
| AML codecs | `scripts/aml_codec.py`, `scripts/aml_codec.js` |
| Formal grammars | `scripts/aml_grammar.lark`, `docs/aml_v2.gbnf` |
| NAM/2 schema | `assets/nam.schema.json` |
| Semantic validation and receiver | `scripts/frame_validation.py`, `scripts/nam.py` |
| File bus and board publishing | `scripts/aml_bus.py`, `scripts/board_frames.py` |
| Automated checks | `tests/`, `verify.py` |
| Traffic generation and measurement | `scripts/benchmark_*.py`, `scripts/bench_bus.py` |

NAM/2 separates the common envelope from the payload. Cluster messages include `HANDOFF`, `RES`, `QUERY`, and `ERR`; `STATE`, `DELTA`, and `PROP` carry structured snapshots, changes, and action proposals. The cluster profile uses `s:2`, which is kept separate from the tactical vocabulary of another profile.

## Installation and offline verification

Requirements: Python 3.10+ and Node.js 18+. The verification suite does not start models or contact model servers.

```sh
python -m venv .venv
# Linux/macOS:
. .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1
python -m pip install -r requirements-test.txt
npm ci
python verify.py
```

Exit code `0` means all enabled offline checks passed; `1` means a check failed; `2` means the run was incomplete because a dependency or tool was missing. `SKIP` is not a pass. The script prints the actual test counts for the checked revision.

## Frame example

```text
!AML:2|RES|aml-nam|codex→claude|2|1790200601000|60000|rel:T13|*REJ:#T13|rs:CRITIC_RETURN

```

The first line contains the wire version, message kind, session, sender and recipient, sequence number, send time, TTL, and payload. A blank line terminates a message in the file transport. Read the [specification](docs/PROTOCOL_SPEC.en.md) before implementing a compatible parser.

## Safety and limitations

- The current schema limits TTL to 60,000 ms; receivers should reject expired messages.
- `Receiver` checks structure, profile, permitted message kinds, freshness, and replay. A board integration must also check the sender's role against the current task state.
- Local models can propose or recommend a verdict; they do not gain permission to change board state.
- The file bus and outbox filename are not network authentication. Do not expose the file bus to an untrusted network. Multi-machine use needs message authentication (for example, HMAC with key management), protected transport, a threat model, and separate implementation review.
- A constrained grammar does not guarantee truthful meaning or safe actions. The application must make decisions through its validated receiver and policy.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Александр Наливайко Павлович. See `LICENSE` for the full terms and warranty disclaimer.

## Version

This repository is based on release r2 with subsequent test and bus fixes. Historical measurements are dated in [Tests and benchmarks](docs/TESTS_AND_BENCHMARKS.en.md); they are not performance guarantees for other hardware or model versions.
