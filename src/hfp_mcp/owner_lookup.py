"""Bounded, read-only owner lookup with the same authority for voice and chat."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime

from .contracts import normalize_phone_number
from .hermes_api import HermesAPI

QUERY_FIELDS = {'number', 'call_id', 'endpoint', 'record_kind', 'start', 'end', 'limit', 'cursor'}
LOOKUP_PROPERTIES = {
    'number': {'type': 'string'}, 'call_id': {'type': 'string'}, 'endpoint': {'type': 'string'},
    'record_kind': {'type': 'string', 'enum': ['notes', 'calls', 'transcripts']},
    'start': {'type': 'string', 'description': 'Inclusive ISO-8601 time with timezone'},
    'end': {'type': 'string', 'description': 'Exclusive ISO-8601 time with timezone'},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}, 'cursor': {'type': 'string'},
}


def validate_query(args, region):
    if not isinstance(args, dict) or set(args) - QUERY_FIELDS:
        raise ValueError('invalid owner lookup arguments')
    query = {**args, 'record_kind': args.get('record_kind', 'notes'), 'limit': args.get('limit', 20)}
    if query['record_kind'] not in {'notes', 'calls', 'transcripts'} or type(query['limit']) is not int or not 1 <= query['limit'] <= 100:
        raise ValueError('record_kind or limit is invalid')
    for key in ('call_id', 'endpoint', 'cursor'):
        if key in query and (not isinstance(query[key], str) or not query[key] or len(query[key]) > 2048):
            raise ValueError('invalid lookup selector')
    if 'number' in query:
        if not isinstance(query['number'], str):
            raise ValueError('number must be text')
        query['number'] = normalize_phone_number(query['number'], region)
    for key in ('start', 'end'):
        if key in query:
            if not isinstance(query[key], str):
                raise ValueError('lookup times must be text')
            value = datetime.fromisoformat(query[key].replace('Z', '+00:00'))
            if value.tzinfo is None:
                raise ValueError('lookup times must include a timezone')
            query[key] = value.timestamp()
    if query.get('start', float('-inf')) >= query.get('end', float('inf')):
        raise ValueError('start must precede end')
    return query


def query_position(query, scope):
    fingerprint = hashlib.sha256(json.dumps({k: v for k, v in query.items() if k != 'cursor'}, sort_keys=True).encode() + scope.encode()).hexdigest()
    position = {'offset': 0, 'text_offset': 0, 'record_hash': None}
    if query.get('cursor'):
        try:
            decoded = json.loads(base64.urlsafe_b64decode(query['cursor']))
            if (decoded.pop('fingerprint') != fingerprint or set(decoded) != set(position)
                    or any(type(decoded[k]) is not int or not 0 <= decoded[k] <= 2**63 - 1 for k in ('offset', 'text_offset'))
                    or (decoded['record_hash'] is not None and not isinstance(decoded['record_hash'], str))):
                raise ValueError()
            position = decoded
        except Exception as exc:
            raise ValueError('cursor does not match this authorized query') from exc
    return fingerprint, position


def page_records(records, query, scope, *, offset_applied=False):
    """Split large text without silently dropping characters or exceeding tool budgets."""
    fingerprint, position = query_position(query, scope)
    offset, text_offset = position['offset'], position['text_offset']
    rows = records if offset_applied else records[offset:]
    result = {'status': 'ok', 'records': [], 'has_more': False, 'cursor': None}
    consumed = 0
    next_text_offset = 0
    record_hash = None
    for row in rows[:query['limit']]:
        entry = dict(row)
        current_hash = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        if consumed == 0 and position['record_hash'] and current_hash != position['record_hash']:
            raise ValueError('record changed; restart lookup without the cursor')
        text_key = 'text' if 'text' in entry else 'notes' if 'notes' in entry else None
        start = text_offset if consumed == 0 else 0
        full_text = entry.get(text_key, '') if text_key else ''
        if start > len(full_text):
            raise ValueError('record changed; restart lookup without the cursor')
        if text_key:
            entry[text_key] = full_text[start:]
            entry['text_offset'] = start
        def fits(item):
            encoded = json.dumps({**result, 'records': [*result['records'], item]}, ensure_ascii=False)
            # The Gemini broker serializes FunctionResponses with ASCII escapes.
            # Leave room for its outcome wrapper instead of losing multilingual records.
            return len(encoded) <= 11000 and len(encoded.encode()) <= 11000 and len(json.dumps({**result, 'records': [*result['records'], item]})) <= 11000
        if not fits(entry):
            if result['records']:
                break
            if not text_key:
                raise ValueError('lookup record exceeds response budget')
            lo, hi = 0, len(full_text) - start
            while lo < hi:
                mid = (lo + hi + 1) // 2
                entry[text_key] = full_text[start:start + mid]
                if fits(entry): lo = mid
                else: hi = mid - 1
            entry[text_key] = full_text[start:start + lo]
            if not lo:
                raise ValueError('lookup metadata exceeds response budget')
            next_text_offset = start + lo
            record_hash = current_hash
            entry['complete'] = False
            result['records'].append(entry)
            break
        if text_key:
            entry['complete'] = True
        result['records'].append(entry)
        consumed += 1
    result['has_more'] = bool(next_text_offset or consumed < len(rows))
    if result['has_more']:
        result['cursor'] = base64.urlsafe_b64encode(json.dumps({
            'fingerprint': fingerprint, 'offset': offset + consumed, 'text_offset': next_text_offset, 'record_hash': record_hash}).encode()).decode()
    return result


async def lookup(config, ledger, origin, authority, args, *, transcripts_enabled):
    query = validate_query(args, config.region)
    if origin not in config.endpoints:
        raise PermissionError('owner endpoint unavailable')
    target = query.get('endpoint', origin)
    permitted = {origin, *config.owner_access.get(origin, ())}
    if target not in permitted or target not in config.endpoints:
        raise PermissionError('owner lookup scope denied')
    if query.get('number'):
        route, _ = config.resolve_outbound(query['number'])
        if not route or route.endpoint != target:
            raise PermissionError('caller is outside the selected owner scope')
    source_api = HermesAPI(config.endpoints[origin])
    target_api = source_api if target == origin else HermesAPI(config.endpoints[target])
    envelope = {'source_endpoint': origin, **authority}
    try:
        async def authorize():
            identity = await source_api.request('POST', 'v1/hfp/owner/authority', bridge=True, json=envelope)
            if identity.get('profile') != config.endpoints[origin].profile or identity.get('authorized') is not True:
                raise PermissionError('owner authority unavailable')
        await authorize()
        historical_numbers = []
        if ledger and not query.get('number'):
            ledger.prune()
            with ledger._lock:
                observed = ledger._db.execute('SELECT number FROM phone_call_sources WHERE endpoint=? AND number IS NOT NULL '
                    'UNION SELECT caller_number FROM call_summary WHERE caller_number IS NOT NULL', (target,)).fetchall()
            for (number,) in observed:
                route, _ = config.resolve_outbound(number)
                if route and route.endpoint == target:
                    historical_numbers.append(number)
        candidates = await target_api.request('POST', 'v1/hfp/owner/notes', bridge=True,
            json={**envelope, 'target_endpoint': target, 'number': query.get('number'), 'numbers': historical_numbers})
        scope = origin + ':' + target + ':' + str(authority.get('binding_id', 'owner-chat'))
        _, position = query_position(query, scope)
        if query['record_kind'] == 'notes':
            records = [r for r in candidates['records'] if
                query.get('start', float('-inf')) <= r['timestamp'] < query.get('end', float('inf'))]
            if query.get('call_id'):
                raise ValueError('Saved notes span calls; use calls or transcripts with call_id')
            result = page_records(records, query, scope)
            result['enumeration'] = 'Exact number when supplied; otherwise configured and retained call numbers'
            if candidates.get('persistent') is False:
                result.update(status='unavailable', message='Persistent memory is disabled for the selected caller')
        elif query['record_kind'] == 'transcripts' and not transcripts_enabled:
            result = {'status': 'unavailable', 'message': 'Full transcript retention is disabled', 'records': [], 'has_more': False, 'cursor': None}
        else:
            if ledger is None:
                raise ValueError('call history unavailable')
            ledger.prune()
            profile = config.endpoints[target].profile
            history = ('WITH history AS (SELECT * FROM phone_call_sources UNION ALL '
                'SELECT t.call_id,c.profile,c.caller_id,?,NULL,c.persistent,MIN(t.created_at) '
                'FROM call_transcript t JOIN phone_conversations c ON c.id=t.conversation_id '
                'WHERE c.profile=? AND ? AND NOT EXISTS (SELECT 1 FROM phone_call_sources s WHERE s.call_id=t.call_id) '
                'GROUP BY t.call_id,c.profile,c.caller_id,c.persistent) ')
            unique_profile_endpoint = sum(e.profile == profile for e in config.endpoints.values()) == 1
            clauses = ['c.profile=?', 'c.endpoint=?', 'c.persistent=1']
            params = [target, profile, int(unique_profile_endpoint), profile, target]
            if query.get('number'):
                if not candidates.get('persistent'):
                    raise PermissionError('caller memory is disabled')
                clauses.append('c.caller_id=?'); params.append(candidates['caller_id'])
            for disabled in candidates.get('disabled_caller_ids', []):
                clauses.append('c.caller_id!=?'); params.append(disabled)
            if query.get('call_id'):
                clauses.append('c.call_id=?'); params.append(query['call_id'])
            for key, sign in [('start', '>='), ('end', '<')]:
                if key in query:
                    clauses.append(('c.created_at' if query['record_kind'] == 'calls' else 't.created_at') + sign + '?'); params.append(query[key])
            where = ' AND '.join(clauses)
            if query['record_kind'] == 'calls':
                sql = history + ('SELECT c.call_id,c.caller_id,c.created_at,MAX(t.created_at),COUNT(t.id),c.number '
                       'FROM history c LEFT JOIN call_transcript t ON t.call_id=c.call_id WHERE ' + where +
                       ' GROUP BY c.call_id,c.caller_id,c.created_at,c.number ORDER BY c.created_at,c.call_id LIMIT ? OFFSET ?')
            else:
                sql = history + ('SELECT t.id,t.call_id,c.caller_id,t.created_at,t.direction,t.text,c.number '
                       'FROM history c JOIN call_transcript t ON t.call_id=c.call_id WHERE ' + where +
                       ' ORDER BY t.id LIMIT ? OFFSET ?')
            with ledger._lock:
                rows = ledger._db.execute(sql, [*params, query['limit'] + 1, position['offset']]).fetchall()
            records = []
            for row in rows:
                if query['record_kind'] == 'calls':
                    record = dict(zip(('call_id', 'caller_id', 'timestamp', 'last_event_at', 'events', 'number'), row))
                else:
                    record = dict(zip(('id', 'call_id', 'caller_id', 'timestamp', 'direction', 'text', 'number'), row))
                records.append({**record, 'profile': config.endpoints[target].profile, 'endpoint': target,
                                'record_kind': query['record_kind'], **({'number': query['number']} if query.get('number') else {})})
            result = page_records(records, query, scope, offset_applied=True)
            result['provenance'] = 'Trusted call/profile association; legacy calls without provenance are unavailable'
        # A delayed response must never outlive its originating authority.
        await authorize()
        if ledger:
            ledger.audit('owner_lookup', 'read', detail={'source_endpoint': origin, 'target_endpoint': target, 'record_kind': query['record_kind']})
        return result
    finally:
        if target_api is not source_api: await target_api.close()
        await source_api.close()
