"""Plain user messages rendered from verified phone state, never raw diagnostics."""
from datetime import datetime
import re
from zoneinfo import ZoneInfo


def subject(purpose):
    text = ' '.join(str(purpose or 'your reminder').split())
    text = re.sub(r'^remind (?:me|the caller|the recipient|you)\s+to\s+', '', text, flags=re.I)
    return text.rstrip('. ') or 'your reminder'


def local_time(instant, timezone):
    value = datetime.fromtimestamp(instant, ZoneInfo(timezone))
    return value.strftime('%-d %b at %-I:%M %p') + ' ' + value.tzname()


def schedule_confirmation(receipts):
    messages = []
    for receipt in receipts:
        if not receipt.get('verified'):
            messages.append("I couldn't confirm the requested schedule. Please check its status before trying again.")
            continue
        due = receipt.get('callback_run_at') or receipt.get('reminder_run_at') or receipt.get('requested_run_at')
        if not isinstance(due, (int, float)):
            messages.append('The schedule was saved. Ask me for its current time and delivery method.')
            continue
        when = local_time(due, receipt.get('timezone') or 'UTC')
        mode = receipt.get('delivery')
        method = {'both': 'by message and phone', 'call': 'by phone', 'gateway': 'by message'}.get(mode, 'by phone')
        messages.append(f"I'll remind you on {when} to {subject(receipt.get('purpose'))}, {method}.")
    return '\n'.join(messages)
