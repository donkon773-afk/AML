# Contributing

Before changing the protocol, read the [protocol specification](docs/PROTOCOL_SPEC.en.md) and check every representation affected by the change: the Python and JavaScript codecs, Lark grammar, GBNF grammar, JSON Schema, and tests. Preserve compatibility or document a breaking change explicitly.

## Verification

```sh
python -m pip install -r requirements-test.txt
npm ci
python verify.py
```

A change is ready for review when the command exits with code `0` and prints `OFFLINE PASSED`. Live model probes and token benchmarks run separately: they require a specified model/backend and are not substitutes for offline tests.

## Security

- Do not commit API keys, passwords, local configuration, `.agent-sync/` contents, or personal data.
- Never execute frame text or LLM output as a command. New actions must remain proposals, pass strict type and authorization checks, and be applied by deterministic code.
- Do not post exploit details or secrets in a public issue. Use the project's agreed private security contact channel.

## License

The project is distributed under the MIT License. The copyright holder and full terms are in [`LICENSE`](LICENSE). Contributions may be offered under the same terms only if the contributor has the right to do so. Do not copy third-party code without checking its license and required notices.
