"""Note formatting and trusted provenance, independent of model-generated text."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# These are our rendered metadata formats, not arbitrary bracketed caller text.
SOURCE = re.compile(r'\[\d{4}-\d\d-\d\dT[^\]\n]*; call [^\]\n]+\]')
LABEL = re.compile(r'^(?:Caller-reported (?:fact|decision|commitment|preference)|Caller note update):\s*', re.I)
DUE = re.compile(r'\s+Due: (\d{4}-\d\d-\d\d)\.?\s*$', re.I)


def clean_fact(text, *, due_date=None):
    """Strip renderer metadata from newly generated fact content only."""
    text = SOURCE.sub('', text).strip().removeprefix('- ').strip()
    while LABEL.match(text):
        text = LABEL.sub('', text, count=1).strip()
    # Strip only the date that will be rendered separately, never a different
    # deadline or a date supplied in the actual prose.
    suffix = ''
    if match := DUE.search(text):
        tail_date = match[1]
        if tail_date != due_date:
            suffix = f' Due: {tail_date}.'
        while (match := DUE.search(text)) and match[1] == tail_date:
            text = text[:match.start()].rstrip()
    text += suffix
    return text


def resolve_dates(text, reported):
    for word, days in [('tomorrow', 1), ('today', 0), ('yesterday', -1)]:
        text = re.sub(r'\b' + word + r'\b', (reported.date() + timedelta(days=days)).isoformat(), text, flags=re.I)
    return text


def source_label(call_id, reported_at, timezone):
    # A model never supplies these values. Reject line/header injection even
    # from a malformed upstream binding rather than rendering forged metadata.
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', call_id):
        raise ValueError('invalid source call ID')
    return f'[{reported_at} {timezone}; call {call_id}]'


def prepare_replacement(old, proposed, binding):
    """Keep unchanged lines verbatim; stamp changed/new lines from the binding."""
    reported = datetime.fromtimestamp(binding['started_at'], ZoneInfo(binding['timezone']))
    source = source_label(binding['call_id'], reported.isoformat(), binding['timezone'])
    existing = set(old.splitlines())
    lines, seen = [], set()
    for line in proposed.splitlines():
        if not line.strip():
            continue
        if line in existing:
            rendered = line
        else:
            text = resolve_dates(clean_fact(line), reported)
            if not text:
                continue
            rendered = f'- {source} Caller note update: {text}'
        if rendered not in seen:
            lines.append(rendered)
            seen.add(rendered)
    return '\n'.join(lines)
