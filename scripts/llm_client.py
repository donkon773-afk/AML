"""Local AML client. Grammar restricts generation; validate syntax, schema and
Receiver semantics after every completed response. Truncation remains an error.
The HTTP grammar parameter must be verified for the particular server version.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aml_codec import AMLCodec, AMLError
import nam

ENDPOINT = "http://127.0.0.1:1234/v1/chat/completions"
GRAMMAR = Path(__file__).resolve().parents[1] / "docs" / "aml_v2.gbnf"
PROMPT = Path(__file__).resolve().parents[1] / "docs" / "AML_SYSTEM_PROMPT.md"


_GRAMMAR_SUPPORT_CACHE: dict[tuple[str, str], bool] = {}


def reset_grammar_support_cache() -> None:
    """Clear probe cache (useful for testing and re-probing)."""
    _GRAMMAR_SUPPORT_CACHE.clear()


def system_prompt() -> str:
    """Pull the fenced block out of the prompt document so the prompt has one
    source of truth instead of a copy that silently drifts."""
    text = PROMPT.read_text(encoding="utf-8")
    start = text.index("```text") + len("```text")
    return text[start:text.index("```", start)].strip()


def ask(user_text: str, *, model: str = "local-model", temperature: float = 0.2,
        grammar: bool = True, timeout: int = 90, grammar_text: str = None, probe_mode: bool = False,
        endpoint: str = ENDPOINT) -> str:
    if grammar and not probe_mode:
        require_grammar_support(model, endpoint=endpoint)

    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": 1024,
        # Qwen burns tokens on <think> and can return an empty answer otherwise.
        "reasoning_effort": "none",
        "messages": [
            {"role": "system", "content": "Follow the user request exactly." if probe_mode else system_prompt()},
            {"role": "user", "content": user_text},
        ],
    }
    if grammar:
        # Endpoint-dependent; test with the random grammar-only challenge.
        payload["grammar"] = grammar_text if grammar_text is not None else GRAMMAR.read_text(encoding="utf-8")

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        answer = json.loads(response.read())
    choice = answer["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise AMLError("Generation did not complete normally: " + str(choice.get("finish_reason")))
    return choice["message"]["content"].strip()


def consume(receiver: nam.Receiver, aml_text: str, peer: str, now_ms: int) -> dict:
    """Decode a model-produced frame and push it through the real receiver."""
    from frame_validation import validate_frame
    message = validate_frame(aml_text)          # complete syntax + schema
    return receiver.accept(json.dumps(message), peer, now_ms)  # step 3


def grammar_selftest(model: str, endpoint: str = ENDPOINT) -> bool:
    from grammar_probe import probe
    ok, report = probe(lambda prompt, text: ask(prompt, model=model, grammar=text is not None,
                      grammar_text=text, probe_mode=True, temperature=0.0, endpoint=endpoint))
    print(report)
    _GRAMMAR_SUPPORT_CACHE[(endpoint, model)] = ok
    return ok


def require_grammar_support(model: str, endpoint: str = ENDPOINT) -> None:
    """Fail closed instead of sending a request falsely labelled constrained."""
    key = (endpoint, model)
    cached = _GRAMMAR_SUPPORT_CACHE.get(key)
    if cached is True:
        return
    if cached is False:
        raise AMLError(
            "HTTP endpoint did not demonstrate grammar enforcement; use the "
            "SDK client for constrained generation or pass --no-grammar for "
            "an explicitly unconstrained request"
        )
    ok = grammar_selftest(model, endpoint=endpoint)
    _GRAMMAR_SUPPORT_CACHE[key] = ok
    if not ok:
        raise AMLError(
            "HTTP endpoint did not demonstrate grammar enforcement; use the "
            "SDK client for constrained generation or pass --no-grammar for "
            "an explicitly unconstrained request"
        )


def demo(now_ms: int = 1_000_000) -> None:
    """Offline walk-through: no model, no network. Shows the two validation
    steps doing their job on a frame that a model might plausibly produce."""
    receiver = nam.Receiver(
        session="s1", recipient="gw",
        permissions={"observer": ["state", "delta"], "planner": ["proposal"]},
    )

    keyframe = (
        "!AML:2|STATE|s1|observer\u2192gw|1|1000000|30000|w:Altis|r:1\n"
        '#A11:grp:INF:20:@1250,3420:%H100A90S0:t1000000:C"\u0410\u043b\u044c\u0444\u0430 1-1"\n'
        "#E02:cnt:INF:6:@1420,3350:%H90A-S-:t999000:src:A11"
    )
    consume(receiver, keyframe, "observer", now_ms)
    print("keyframe accepted, entities:", sorted(receiver.states["observer"]["entities"]))

    proposal = (
        "!AML:2|PROP|s1|planner\u2192gw|2|1000000|30000|w:Altis|r:1|by:observer|rs:CONTACT[#E02]\n"
        "$ATK:#A11:@1420,3350:#op1\n"
        "$ROE:#A11:OPEN_FIRE:#op2"
    )
    result = consume(receiver, proposal, "planner", now_ms)
    print("proposal accepted, actions:", [a["type"] for a in result["body"]["actions"]])

    # A hallucinated target: grammatically perfect, semantically empty.
    ghost = (
        "!AML:2|PROP|s1|planner\u2192gw|3|1000000|30000|w:Altis|r:1|by:observer|rs:CONTACT[#E02]\n"
        "$ATK:#A99:@1420,3350:#op1"
    )
    try:
        consume(receiver, ghost, "planner", now_ms)
    except nam.ProtocolError as exc:
        print("ghost target rejected by receiver:", exc)

    # A frame missing the observation timestamp: caught one layer earlier.
    stale = (
        "!AML:2|STATE|s1|observer\u2192gw|4|1000000|30000|w:Altis|r:2\n"
        "#A11:grp:INF:20:@1250,3420:%H100A90S0"
    )
    try:
        consume(receiver, stale, "observer", now_ms)
    except AMLError as exc:
        print("missing t<ms> rejected by codec:", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ask", help="send this text to the local model")
    parser.add_argument("--model", default="local-model")
    parser.add_argument("--no-grammar", action="store_true",
                        help="disable GBNF constraint, to measure how often the "
                             "model gets the format right unaided")
    parser.add_argument("--demo", action="store_true",
                        help="offline validation walk-through, no model needed")
    parser.add_argument("--selftest-grammar", action="store_true",
                        help="check whether the endpoint really enforces GBNF")
    args = parser.parse_args()

    if args.selftest_grammar:
        try:
            sys.exit(0 if grammar_selftest(args.model) else 1)
        except (urllib.error.URLError, AMLError) as exc:
            parser.exit(1, f"grammar self-test failed at {ENDPOINT}: {exc}\n")

    if args.demo or not args.ask:
        demo()
        return

    try:
        if not args.no_grammar:
            require_grammar_support(args.model)
        raw = ask(args.ask, model=args.model, grammar=not args.no_grammar)
    except (urllib.error.URLError, AMLError) as exc:
        parser.exit(1, f"model request rejected: {exc}\n")

    print(raw)
    try:
        from frame_validation import validate_frame
        print(json.dumps(validate_frame(raw), ensure_ascii=False, indent=2))
    except Exception as exc:
        parser.exit(1, f"model produced an unusable frame: {exc}\n")


if __name__ == "__main__":
    main()
