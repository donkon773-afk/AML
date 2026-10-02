"""Full syntax + NAM-schema validation; does not replace Receiver semantics."""
from functools import lru_cache
from pathlib import Path
from lark import Lark
from aml_codec import AMLCodec, AMLError
from lark.exceptions import UnexpectedInput
import nam


@lru_cache(maxsize=1)
def parser():
    return Lark(Path(__file__).with_name('aml_grammar.lark').read_text(encoding='utf-8'), parser='earley')


def validate_frame(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > nam.MAX_BYTES:
        raise nam.ProtocolError('expected bounded AML text')
    try:
        parser().parse(raw.strip())
    except UnexpectedInput as exc:
        raise AMLError("Invalid or incomplete AML syntax") from exc
    message = AMLCodec.decode(raw)
    nam.encode(message)
    return message
