"""Conservative fallback for finalized, literal physical-call commands."""
import re


def literal_hangup(text: str, *, fictional: bool = False) -> bool:
    if fictional or len(text) > 160:
        return False
    text = text.strip().casefold()
    # Full-match grammar deliberately excludes quoting, negation, questions,
    # conditions and descriptions of what somebody else said.
    if any(c in text for c in '\"“”‘’\'?:'):
        return False
    parts = [p.strip(' ,') for p in re.split(r'[.!]+', text) if p.strip(' ,')]
    command = re.compile(r'(?:please )?(?:(?:disconnect|end|cut) (?:this |the )?(?:phone )?call(?: now)?|hang up(?: (?:this|the) call)?(?: now)?|we can cut the call|കോൾ (?:കട്ട് ചെയ്യൂ|കട്ട് ചെയ്യുക|നിർത്തൂ)|കാൾ കട്ട് ചെയ്യൂ)(?: please)?')
    return bool(parts) and all(command.fullmatch(p) for p in parts)
