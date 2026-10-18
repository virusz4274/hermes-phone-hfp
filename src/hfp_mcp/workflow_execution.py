"""Daemon execution receipts. No scheduling or model decisions live here."""
from __future__ import annotations
import asyncio
import json
import time
from .contracts import normalize_phone_number


class WorkflowExecution:
    def __init__(self, runtime, config):
        self.runtime, self.config = runtime, config
        self.tasks = set()
        ledger = runtime.ledger()
        with ledger._lock, ledger._db:
            ledger._db.execute('CREATE TABLE IF NOT EXISTS phone_workflow_execution (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            for key, data in ledger._db.execute('SELECT id,data FROM phone_workflow_execution').fetchall():
                row = json.loads(data)
                if row['status'] == 'starting':
                    row['status'] = 'uncertain'
                    self.save(row)

    def get(self, key):
        ledger = self.runtime.ledger()
        with ledger._lock:
            found = ledger._db.execute('SELECT data FROM phone_workflow_execution WHERE id=?', (key,)).fetchone()
            return json.loads(found[0]) if found else None

    def save(self, row):
        ledger = self.runtime.ledger()
        with ledger._lock, ledger._db:
            ledger._db.execute('INSERT OR REPLACE INTO phone_workflow_execution VALUES (?,?)', (row['id'], json.dumps(row)))

    async def start(self, body):
        from .routing import RoutingConfig
        from .hermes_api import HermesAPI
        from .contracts import validate_request_id
        if set(body) != {'endpoint', 'callback_id', 'attempt_id', 'execution_id'}:
            raise ValueError('invalid phone execution envelope')
        if not isinstance(body['callback_id'], str) or not body['callback_id'].startswith('cb-'):
            raise ValueError('invalid callback identity')
        validate_request_id(body['callback_id'][3:])
        for key in ('attempt_id', 'execution_id'):
            validate_request_id(body[key])
        routing = RoutingConfig.load()
        endpoint = routing.endpoints[body['endpoint']]
        api = HermesAPI(endpoint)
        try:
            grant = await api.request('POST', 'v1/hfp/workflows/' + body['callback_id'] + '/authorize', bridge=True,
                                      json={'attempt_id': body['attempt_id'], 'execution_id': body['execution_id']})
        finally:
            await api.close()
        number = normalize_phone_number(grant['number'], routing.region)
        route, _ = routing.resolve_outbound(number)
        if (not grant.get('authorized') or grant.get('profile') != endpoint.profile
                or grant.get('attempt_id') != body['attempt_id'] or not route
                or routing.endpoints[route.endpoint].profile != endpoint.profile
                or time.time() >= grant['deadline']):
            raise PermissionError('live phone execution scope unavailable')
        ledger = self.runtime.ledger()
        with ledger._lock, ledger._db:
            row = self.get(body['attempt_id'])
            if row:
                if row['envelope'] != body:
                    raise PermissionError('attempt identity changed')
                return {'ok': True, 'status': row['status'], 'replayed': True}
            row = {'id': body['attempt_id'], 'envelope': body, 'number': number, 'profile': endpoint.profile,
                   'started_at': time.time(), 'call_generation': self.runtime.state.call_generation + 1,
                   'call_id': None, 'status': 'starting', 'result': None}
            self.save(row)
        task = asyncio.create_task(self.dial(row, grant['purpose']))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return {'ok': True, 'status': 'starting'}

    async def dial(self, row, purpose):
        try:
            launched = asyncio.create_task(self.runtime.start_call(row['number'], row['id'], purpose, workflow_brief=True))
            while not launched.done():
                state = self.runtime.state
                if (state.call_generation == row['call_generation'] and state.call_id and not row['call_id']
                        and getattr(state.call_direction, 'name', '') == 'OUTGOING' and state.remote_number == row['number']):
                    row['call_id'] = state.call_id
                    self.save(row)
                await asyncio.wait({launched}, timeout=0.05)
            row['result'] = await launched
            row['call_id'] = row['call_id'] or row['result'].get('result', {}).get('call_id')
            row['status'] = 'observing' if row['call_id'] else 'failed'
            self.save(row)
        except asyncio.CancelledError:
            row['status'] = 'uncertain'
            self.save(row)
            raise
        except Exception:
            row['status'] = 'uncertain'
            self.save(row)

    def outcome(self, key):
        row = self.get(key)
        if not row:
            return {'finished': False, 'missing': True}
        if row.get('outcome'):
            return {'finished': True, 'outcome': row['outcome']}
        ledger = self.runtime.ledger()
        with ledger._lock:
            if not row['call_id']:
                ended = ledger._db.execute("SELECT call_id,detail_json FROM call_audit WHERE action='call_ended' AND caller_number=? AND created_at>=? ORDER BY id", (row['number'], row['started_at'])).fetchall()
                matching = [(cid, json.loads(detail)) for cid, detail in ended
                            if json.loads(detail).get('call_generation') == row['call_generation']]
                if len(matching) == 1:
                    row['call_id'] = matching[0][0]
                    self.save(row)
            ended = ledger._db.execute("SELECT detail_json,created_at FROM call_audit WHERE call_id=? AND action='call_ended' ORDER BY id DESC LIMIT 1", (row['call_id'],)).fetchone() if row['call_id'] else None
        active = self.runtime.state.call_id == row['call_id'] and row['call_id'] is not None
        if not ended and (active or row['status'] == 'starting'):
            return {'finished': False, 'active': bool(active), 'call_id': row['call_id']}
        reason = json.loads(ended[0]).get('termination_reason', 'disconnected') if ended else 'unknown'
        result = row['result'] or {}
        code = (result.get('error') or {}).get('code') if isinstance(result.get('error'), dict) else None
        connected = bool(result.get('ok'))
        uncertain = row['status'] == 'uncertain' or (not ended and code not in {'timeout_cancelled', 'call_conflict', 'phone_routing_disabled', 'phone_route_unavailable', 'command_rejected'})
        if ended:
            connected = connected or bool(json.loads(ended[0]).get('was_active'))
            uncertain = reason == 'transport_disconnected'
        if not ended and code == 'timeout_cancelled':
            reason = 'answer_timeout_cancelled'
        elif not ended and code:
            reason = code
        if ended and not connected and not uncertain and reason == 'disconnected':
            reason = 'no_answer'
        evidence, actions = [], []
        if row['call_id']:
            page = ledger.transcript(row['call_id'], limit=100)
            budget = 12000
            for event in page['events']:
                text = event['text'][:budget]
                if not text:
                    break
                evidence.append({'id': 'transcript:' + str(event['id']), 'kind': 'transcript',
                                 'direction': event['direction'], 'text': text, 'timestamp': event['timestamp']})
                budget -= len(text)
            with ledger._lock:
                receipts = ledger._db.execute("SELECT id,action,detail_json FROM call_audit WHERE call_id=? AND outcome='ok' AND action NOT IN ('call_ended','phone_timing','place_call','record_call_summary') ORDER BY id LIMIT 100", (row['call_id'],)).fetchall()
            for rid, action, detail in receipts:
                detail_data = json.loads(detail)
                if action == 'phone_tool_receipt' and not detail_data.get('verified_write'):
                    continue
                actions.append({'id': 'action:' + str(rid), 'action': action, 'receipt': json.loads(detail)})
            evidence.extend({'id': a['id'], 'kind': 'verified_action', 'action': a['action']} for a in actions)
        if connected and not any(e.get('direction') == 'input' for e in evidence) and not actions:
            uncertain = True
        outcome = {'connection_state': 'connected' if connected else 'uncertain' if uncertain else 'not_connected',
                   'termination_reason': reason, 'uncertain': uncertain, 'still_active': bool(active),
                   'call_id': row['call_id'], 'call_generation': row['call_generation'],
                   'profile': row['profile'], 'attempt_id': key, 'started_at': row['started_at'],
                   'ended_at': ended[1] if ended else time.time(), 'evidence': evidence, 'verified_actions': actions}
        if row['call_id']:
            outcome['evidence_truncated'] = bool(page['has_more'] or budget <= 0)
        if ended or not uncertain:
            row['status'], row['outcome'] = 'finished', outcome
            self.save(row)
        return {'finished': True, 'outcome': outcome}
