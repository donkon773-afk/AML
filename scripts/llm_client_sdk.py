"""AML SDK client candidate, pinned to lmstudio-python 1.5.0.
Full text/schema validation and a normal completion reason are mandatory.
Server integration and actual grammar enforcement require live verification.
The random probe tests the route, not the correctness of AML decisions.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aml_codec import AMLCodec, AMLError
from llm_client import system_prompt, GRAMMAR

try:
    import lmstudio as lms
except ImportError:
    sys.exit("pip install lmstudio")

# Reasoning models return their deliberation inside `content`, terminated by a
# synthetic separator. PredictionResult has no separate reasoning field, so the
# only way to get at the answer is to split on that marker. The hash varies per
# response, hence the pattern.
#
# Splitting must not be greedy. Under an enforced grammar there is no reasoning
# at all, and LM Studio can still append the marker after the answer -- a
# greedy ".*MARKER" then deletes the entire frame and leaves an empty string,
# which looks exactly like "the model returned nothing".
REASONING_MARKER = re.compile(
    r"__LM_STUDIO_INTERNAL_LSEP_SYNTHETIC_REASONING_END_[0-9a-f]+__")


def strip_reasoning(text: str) -> str:
    parts = REASONING_MARKER.split(text)
    if len(parts) == 1:
        return text.strip()
    # Normally: [reasoning, answer]. If the tail is empty the marker was a
    # trailer, not a separator, so the answer is what came before it.
    tail = parts[-1].strip()
    return tail or parts[0].strip()


# One worked example. Three different models answered the spec-only prompt by
# reciting the spec back, so a baseline without an example measures prompt
# quality rather than the model. Under a grammar this is redundant; without one
# it is the difference between a fair test and a rigged one.
EXAMPLE_IN = ("Build one STATE frame from this situation. Output the frame only.\n"
              "обстановка: отделение B22, 8 человек, координаты 900,1100, "
              "HP 80%, боезапас 50%, наблюдалось в 999000")
EXAMPLE_OUT = ("!AML:2|STATE|s1|obs\u2192gw|1|999000|30000|w:Altis|r:1\n"
               "#B22:grp:INF:8:@900,1100:%H80A50S0:t999000")


def ask(model_id: str, user_text: str, *, grammar: bool = True, verbose: bool = False,
        max_tokens: int = 600, no_think: bool = False, example: bool = False,
        grammar_text: str = None, probe_mode: bool = False,
        system_text: str = None) -> str:
    model = lms.llm(model_id)
    # Qwen3-family models treat "/no_think" as a switch that skips deliberation.
    # It has to sit in the system message: appended to the user text it reads as
    # part of the data, and the model answers about it instead of obeying it.
    if system_text is not None:
        system = system_text + ("\n/no_think" if no_think else "")
    else:
        system = ("Follow the user request exactly." if probe_mode else system_prompt()) + ("\n/no_think" if no_think else "")
    chat = lms.Chat(system)
    if example:
        chat.add_user_message(EXAMPLE_IN)
        chat.add_assistant_response(EXAMPLE_OUT)
    chat.add_user_message(user_text)
    kwargs = {}
    if grammar:
        kwargs["response_format"] = {
            "type": "gbnf",
            "gbnfGrammar": grammar_text if grammar_text is not None else GRAMMAR.read_text(encoding="utf-8"),
        }

    # In GBNF mode LM Studio routes the text into `parsed` rather than
    # `content`, so str(result) can come back empty even though generation
    # clearly happened (lmstudio-python issue #59). Accumulating the streamed
    # fragments is the one source that is right in every mode.
    seen = {"n": 0, "text": "", "head_shown": False}

    def collect(fragment):
        seen["n"] += 1
        seen["text"] += fragment.content
        if verbose:
            if not seen["head_shown"] and len(seen["text"]) >= 40:
                seen["head_shown"] = True
                print(f"  first 40 chars: {seen['text'][:40]!r}",
                      file=sys.stderr, flush=True)
            if seen["n"] % 50 == 0:
                print(f"  ... {seen['n']} tokens", file=sys.stderr, flush=True)

    kwargs["on_prediction_fragment"] = collect

    # Without a cap a grammar-constrained model can run to the context limit:
    # the entity and action lists are repetitions, so "one more row" stays
    # legal until the bound is reached.
    kwargs["config"] = {"maxTokens": max_tokens}

    result = model.respond(chat, **kwargs)
    text = seen["text"] or str(getattr(result, "parsed", "") or result)
    stop = getattr(getattr(result, "stats", None), "stop_reason", None)
    if stop not in ("eosFound", "stopStringFound"):
        raise AMLError("SDK completion not confirmed: " + str(stop))
    return strip_reasoning(text)


def selftest(model_id: str, *, max_tokens: int = 1024,
             no_think: bool = False) -> bool:
    from grammar_probe import probe
    print("Running control and two random grammar-only probes...", file=sys.stderr, flush=True)
    ok, report = probe(lambda prompt, text: ask(model_id, prompt, grammar=text is not None,
                      grammar_text=text, probe_mode=True, max_tokens=max_tokens,
                      no_think=no_think))
    print(report)
    return ok


FRAME_START = re.compile(r"^!AML:2\|", re.MULTILINE)


def extract_frame(text: str) -> str:
    """Cut out the first AML frame, ignoring anything wrapped around it.

    Two questions hide behind "did the model get the format right", and they
    deserve separate numbers:
      strict  -- the whole answer is a frame, which is what a pipeline needs
      lenient -- a valid frame is in there somewhere, behind reasoning, prose
                 or code fences, and could be recovered with a regex
    Reporting only the strict number understates the model; reporting only the
    lenient one hides the post-processing you would have to write.
    """
    match = FRAME_START.search(text)
    return text[match.start():].strip() if match else ""


def baseline(model_id: str, runs: int, max_tokens: int = 2000,
             no_think: bool = False, example: bool = True) -> None:
    """Measure unconstrained full syntax/schema validity on this fixture.
    This is not a decision-quality test or a constrained-generation benchmark.

    A flat 0/N is worth inspecting rather than believing: it can mean the model
    never gets the format right, or it can mean the harness is failing for some
    unrelated reason (a reasoning model running out of tokens before it even
    starts answering, for instance). The first failure is printed in full so
    the two cases can be told apart."""
    prompt = ("Build one STATE frame from this situation. Output the frame only.\n"
              "обстановка: отделение A11, 20 человек, координаты 1250,3420, "
              "HP 100%, боезапас 90%, наблюдалось в 1000000")
    strict = lenient = 0
    first_failure = None

    for i in range(1, runs + 1):
        raw = ""
        try:
            raw = ask(model_id, prompt, grammar=False, max_tokens=max_tokens,
                      no_think=no_think, example=example)
            try:
                from frame_validation import validate_frame
                validate_frame(raw)
                strict += 1
                lenient += 1
            except Exception as exc:
                frame = extract_frame(raw)
                if frame:
                    try:
                        validate_frame(frame)
                        lenient += 1
                    except Exception:
                        pass
                if first_failure is None:
                    first_failure = (type(exc).__name__, str(exc), raw)
        except Exception as exc:
            if first_failure is None:
                first_failure = (type(exc).__name__, str(exc), raw)
        print(f"  {i}/{runs} strict={strict} lenient={lenient}")

    pct = lambda n: f"{n}/{runs} = {100 * n / runs:.1f}%"
    print(f"\nwithout grammar, whole answer is a valid frame: {pct(strict)}")
    print(f"without grammar, a valid frame can be extracted: {pct(lenient)}")
    print("Constrained completion/validity/quality rates must be measured separately.")
    if first_failure:
        kind, message, raw = first_failure
        print(f"\nfirst failure was {kind}: {message}")
        print("model returned:")
        print("-" * 60)
        print(raw[:600] if raw else "(empty)")
        print("-" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="exact model id from /v1/models")
    parser.add_argument("--ask")
    parser.add_argument("--selftest-grammar", action="store_true")
    parser.add_argument("--baseline", type=int, metavar="N",
                        help="run N unconstrained generations and count valid frames")
    parser.add_argument("--no-example", action="store_true",
                        help="drop the one-shot example from the baseline "
                             "prompt, to measure the spec-only case")
    parser.add_argument("--no-think", action="store_true",
                        help="append /no_think so a reasoning model answers "
                             "directly (Qwen3 family)")
    parser.add_argument("--max-tokens", type=int, default=2000,
                        help="token cap per generation; reasoning models need "
                             "room to finish thinking before they answer")
    args = parser.parse_args()

    if args.selftest_grammar:
        sys.exit(0 if selftest(args.model, max_tokens=args.max_tokens,
                               no_think=args.no_think) else 1)
    if args.baseline:
        baseline(args.model, args.baseline, max_tokens=args.max_tokens,
                 no_think=args.no_think, example=not args.no_example)
        return
    if not args.ask:
        parser.error("give --ask, --selftest-grammar or --baseline")

    raw = ask(args.model, args.ask, max_tokens=args.max_tokens,
              no_think=args.no_think)
    print(raw)
    try:
        from frame_validation import validate_frame
        print(json.dumps(validate_frame(raw), ensure_ascii=False, indent=2))
    except Exception as exc:
        sys.exit(f"model produced an unusable frame: {exc}")


if __name__ == "__main__":
    main()
