"""Random grammar-only challenge. No live engine is exercised by unit tests."""
import secrets


def probe(generate):
    """generate(prompt, grammar_text) -> text; None means unconstrained.

    A marker is disclosed only to the grammar, never in any prompt. Two exact
    matches establish evidence for enforcement on this route, not a guarantee
    of correctness/completion of arbitrary AML messages.
    """
    prompt = "Reply with exactly HELLO WORLD and nothing else."
    control = generate(prompt, None).strip()
    if control != "HELLO WORLD":
        return False, "INCONCLUSIVE: unconstrained control did not follow the probe prompt"
    for _ in range(2):
        marker = "AML_PROBE_" + secrets.token_hex(12)
        grammar = 'root ::= "' + marker + '"'
        if generate(prompt, grammar).strip() != marker:
            return False, "FAIL: grammar-only marker was not returned in full"
    return True, "PASS: two random grammar-only probes passed; AML still requires full validation"
