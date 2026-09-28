"""Durable phone task coordination inside the existing Hermes gateway.

Runs, execution, approvals, steering, stopping and messaging remain native.
This registry records ownership and lifetime; it never replays work at startup.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import suppress

from .hermes_api import HermesAPI

TERMINAL = {"completed", "failed", "cancelled", "interrupted", "uncertain"}


class TaskCapacity:
    """Atomic host-wide slots in the existing daemon ledger, including other gateways."""
    def __init__(self, path, owner):
        self.owner = owner
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, check_same_thread=False)
        path.chmod(0o600)
        self.lock = threading.RLock()
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS phone_task_slots (task_id TEXT PRIMARY KEY, owner TEXT NOT NULL)")

    def reserve(self, task_id, limit):
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                if self.db.execute("SELECT 1 FROM phone_task_slots WHERE task_id=?", (task_id,)).fetchone():
                    return True
                if self.db.execute("SELECT count(*) FROM phone_task_slots").fetchone()[0] >= limit:
                    return False
                self.db.execute("INSERT INTO phone_task_slots VALUES (?,?)", (task_id,self.owner))
                return True
            finally:
                self.db.commit()

    def release(self, task_id):
        with self.lock, self.db:
            self.db.execute("DELETE FROM phone_task_slots WHERE task_id=? AND owner=?", (task_id,self.owner))

    def reconcile(self, rows):
        active = {r['task_id'] for r in rows if r['status'] not in TERMINAL}
        with self.lock, self.db:
            for (key,) in self.db.execute("SELECT task_id FROM phone_task_slots WHERE owner=?", (self.owner,)).fetchall():
                if key not in active: self.db.execute("DELETE FROM phone_task_slots WHERE task_id=?", (key,))
            for key in active: self.db.execute("INSERT OR IGNORE INTO phone_task_slots VALUES (?,?)", (key,self.owner))


class TaskRegistry:
    def __init__(self, store):
        self.store = store
        with store._lock, store.db:
            store.db.executescript('''
                CREATE TABLE IF NOT EXISTS phone_tasks (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    root_id TEXT NOT NULL, parent_binding TEXT NOT NULL,
                    binding_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS phone_task_sessions (
                    conversation_id TEXT NOT NULL, root_id TEXT PRIMARY KEY);
                CREATE INDEX IF NOT EXISTS phone_task_run ON phone_tasks(json_extract(data, '$.run_id'));
            ''')

    def rows(self):
        with self.store._lock:
            return [json.loads(r[0]) for r in self.store.db.execute("SELECT data FROM phone_tasks ORDER BY rowid")]

    def get(self, task_id):
        with self.store._lock:
            row = self.store.db.execute("SELECT data FROM phone_tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, row):
        with self.store._lock, self.store.db:
            self.store.db.execute("INSERT INTO phone_tasks VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (row['task_id'], row['conversation_id'], row['session_id'], row['parent_binding'], row['binding_id'], json.dumps(row)))

    def update(self, task_id, **fields):
        with self.store._lock:
            row = self.get(task_id)
            row.update(fields)
            self.save(row)
            return row

    def roots(self, conversation_id):
        with self.store._lock:
            return [r[0] for r in self.store.db.execute("SELECT root_id FROM phone_task_sessions WHERE conversation_id=?", (conversation_id,))]

    def link(self, conversation_id, root):
        with self.store._lock, self.store.db:
            self.store.db.execute("INSERT OR IGNORE INTO phone_task_sessions VALUES (?,?)", (conversation_id, root))

    def public(self, row):
        return {k: row[k] for k in ('task_id', 'request_id', 'label', 'session_id', 'run_id', 'status', 'message',
                'continue_after_call', 'expires_at', 'delivery', 'approval', 'pending_steer') if k in row}

    def authority(self, run_id):
        with self.store._lock:
            result = self.store.db.execute("SELECT data FROM phone_tasks WHERE json_extract(data, '$.run_id')=?", (run_id,)).fetchone()
        if result:
            row = json.loads(result[0])
            if row['status'] in TERMINAL or row['status'] == 'stopping':
                raise PermissionError('phone task no longer has authority')
            if row['continue_after_call']:
                if row['expires_at'] <= time.time():
                    raise PermissionError('continued task authorization expired')
            else:
                self.store.binding(row['parent_binding'])
            return row
        return None


class GatewayTasks:
    def __init__(self, bridge, adapter, peers, admission_lock, *, api=None, capacity_path=None):
        self.bridge, self.store, self.adapter = bridge, bridge.store, adapter
        self.registry = TaskRegistry(self.store)
        self.capacity = TaskCapacity(capacity_path or self.store.path, str(self.store.path.resolve()))
        self.peers, self.admission_lock = peers, admission_lock
        self.api = api
        self.worker = None
        self.children = set()
        self.streams = {}

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.children.add(task)
        task.add_done_callback(self.children.discard)
        task.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        return task

    async def start(self, _app=None):
        if self.api is None:
            endpoint = next(e for e in self.bridge.config.endpoints.values() if e.profile == self.bridge.profile)
            self.api = HermesAPI(endpoint)
        # A reserved task without a recorded run may have crossed the native
        # admission boundary. Never turn that uncertainty into a second action.
        for row in self.registry.rows():
            if row['status'] not in TERMINAL and not row.get('run_id'):
                self.registry.update(row['task_id'], status='uncertain',
                    message='Gateway restarted during admission; execution is uncertain. Do not resubmit automatically.')
                self.store.revoke(row['binding_id'])
        self.capacity.reconcile(self.registry.rows())
        self.worker = asyncio.create_task(self.watch(), name='phone-native-run-observer')

    async def close(self, _app=None):
        for task in [self.worker, *self.children]:
            if task: task.cancel()
        await asyncio.gather(*[t for t in [self.worker, *self.children] if t], return_exceptions=True)
        if self.api: await self.api.close()

    def owned(self, binding_id, conversation_id):
        binding = self.store.binding(binding_id)
        if binding['policy'].get('notes_only'):
            raise PermissionError('conversation-only calls cannot create native tasks')
        if binding['profile'] != self.bridge.profile:
            raise PermissionError('wrong profile')
        with self.store._lock:
            root = self.store.db.execute('SELECT root_id FROM managed_sessions WHERE conversation_id=?', (conversation_id,)).fetchone()
        if not root:
            raise PermissionError('unknown phone conversation')
        self.store.check_managed(root[0], binding_id)
        return binding, root[0]

    def continuation_allowed(self, binding):
        if not binding['policy'].get('admin') or not binding['policy'].get('background_tasks') or not binding['persistent']:
            raise PermissionError('continued tasks require the configured admin policy and a known caller')
        # Validate the native configured destination before promising delivery.
        from gateway.config import load_gateway_config, Platform
        with self.adapter._profile_scope(self.bridge.profile):
            cfg = load_gateway_config()
            platform = cfg.platforms.get(Platform.TELEGRAM)
            if not platform or not platform.enabled or not cfg.get_home_channel(Platform.TELEGRAM):
                raise ValueError('Hermes Telegram home channel is not configured or enabled')

    async def submit(self, body):
        async with self.admission_lock:
            binding_id, conversation = body['binding_id'], body['conversation_id']
            binding, primary = self.owned(binding_id, conversation)
            key = str(body['request_id'])
            text = body['text']
            if not key or len(key) > 200 or not isinstance(text, str) or not text.strip() or len(text) > 48000:
                raise ValueError('invalid task request')
            task_id = hashlib.sha256((binding_id + ':' + key).encode()).hexdigest()[:32]
            fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            old = self.registry.get(task_id)
            if old:
                if old['fingerprint'] != fingerprint:
                    raise ValueError('request ID reused with different task')
                return self.registry.public(old)
            relation = body.get('relationship', 'new')
            continued = body.get('continue_after_call', False)
            if not isinstance(continued, bool) or relation not in {'new', 'independent', 'follow_up', 'correction', 'replace'}:
                raise ValueError('invalid task intent')
            if continued: self.continuation_allowed(binding)
            rows = [r for r in self.registry.rows() if r['conversation_id'] == conversation]
            target = next((r for r in rows if r['task_id'] == body.get('task_id')), None)
            if body.get('task_id') and not target: raise PermissionError('unknown caller task')
            active = [r for r in rows if r['status'] not in TERMINAL]
            if relation in {'follow_up', 'correction', 'replace'}:
                if not target and len(active) == 1: target = active[0]
                if not target:
                    raise ValueError('Select the original task before correcting, replacing or following up.')
                if target['status'] not in TERMINAL:
                    if not target.get('run_id'):
                        return {**self.registry.public(target), 'message': 'Admission is not confirmed yet. No correction or replacement has been applied; check status shortly.'}
                    if relation == 'replace':
                        await self.stop(target)
                        return {'status': 'stopping', 'task_id': target['task_id'],
                            'message': 'The original task is stopping. Replacement has not started; check status before submitting it.'}
                    result = await self.api.steer(target['run_id'], text)
                    if not result.get('accepted'): raise ValueError('native steering not accepted')
                    return {**self.registry.public(target), 'message': 'Correction accepted for delivery; not yet confirmed applied.'}
                root = target['session_id']
            else:
                root = primary
                if active and relation != 'independent':
                    raise ValueError('A task is active. Identify an independent request or select the original task for a correction.')
            total = sum(r['status'] not in TERMINAL for peer in self.peers for r in peer.registry.rows())
            if total >= self.bridge.config.max_phone_tasks:
                return {'status': 'busy', 'message': 'Phone task capacity is full. Nothing was queued; offer to cancel or replace a selected task.'}
            busy_roots = {r['session_id'] for r in active}
            if root in busy_roots:
                if relation != 'independent': raise ValueError('The selected task session is occupied.')
                root = next((r for r in self.registry.roots(conversation) if r not in busy_roots), None)
                if root is None:
                    root = self.store.manage(secrets.token_hex(16), binding_id)
                    self.registry.link(conversation, root)
            from . import hermes_sessions
            await asyncio.to_thread(hermes_sessions.create, self.store, root, self.bridge.profile)
            child = 'hfp-' + secrets.token_hex(16)
            # The run hook checks the parent call or bounded continuation grant.
            # This unrenewable child cannot be used to admit unrelated work.
            self.store.bind(session_id=child, call_id=binding['call_id'], profile=binding['profile'],
                number=None, policy=binding['policy'], ttl=86400, anonymous_identity=binding['caller_id'])
            with self.store._lock, self.store.db:
                self.store.db.execute('UPDATE bindings SET persistent=? WHERE session_id=?', (int(binding['persistent']), child))
            row = dict(task_id=task_id, request_id=key, conversation_id=conversation, session_id=root, parent_binding=binding_id,
                binding_id=child, fingerprint=fingerprint, status='submitting', created_at=time.time(),
                continue_after_call=continued, expires_at=time.time()+self.bridge.config.background_task_minutes*60 if continued else None,
                label=str(body.get('label') or 'Phone task')[:120], delivery='pending' if continued else 'not_requested',
                message='Submitting to Hermes; acceptance and completion are not yet confirmed.')
            if not await asyncio.to_thread(self.capacity.reserve, task_id, self.bridge.config.max_phone_tasks):
                self.store.revoke(child)
                return {'status': 'busy', 'message': 'Phone task capacity is full across gateways. Nothing was queued; offer cancellation or replacement.'}
            try:
                self.registry.save(row)
            except Exception:
                self.capacity.release(task_id)
                self.store.revoke(child)
                raise
            self.spawn(self.launch(row, text, binding['caller_id']))
            return self.registry.public(row)

    async def launch(self, row, text, caller_id):
        headers = {'Idempotency-Key': 'hfp-task-' + row['task_id'],
                   'X-Hermes-Session-Key': 'hfp:' + self.bridge.profile + ':' + caller_id,
                   'X-HFP-Binding': row['binding_id'], 'X-HFP-Task': row['task_id']}
        try:
            response = await self.api.request('POST', 'v1/runs', bridge=True, headers=headers,
                json={'session_id': row['session_id'], 'input': text})
            # Middleware persists the ID before the native worker can execute.
            current = self.registry.get(row['task_id'])
            if not current.get('run_id'):
                raise RuntimeError('native admission did not bind phone task')
        except asyncio.CancelledError:
            raise
        except Exception:
            current = self.registry.get(row['task_id'])
            if not current.get('run_id'):
                self.registry.update(row['task_id'], status='uncertain',
                    message='Hermes admission was not confirmed. Do not repeat this action automatically.')
                self.store.revoke(row['binding_id'])
                self.capacity.release(row['task_id'])

    def admitted(self, task_id, run_id, binding_id, root):
        row = self.registry.get(task_id)
        if not row or row['binding_id'] != binding_id or row['session_id'] != root:
            raise PermissionError('task admission mismatch')
        if row.get('run_id') and row['run_id'] != run_id: raise PermissionError('run cannot be replaced')
        if row['status'] in TERMINAL or row['status'] == 'stopping':
            self.registry.update(task_id, run_id=run_id, status='stopping')
            self.spawn(self.api.stop(run_id))
            raise PermissionError('task already revoked')
        self.registry.update(task_id, run_id=run_id, status='accepted', message='Hermes accepted the task; it has not completed.')

    async def stop(self, row, reason=None):
        if row['status'] in TERMINAL: return self.registry.public(row)
        self.registry.update(row['task_id'], status='stopping', continue_after_call=False, delivery='cancelled',
            stop_reason=reason or 'Cancellation requested; completed actions are not undone.',
            message=reason or 'Cancellation requested; completed actions are not undone.')
        self.store.revoke(row['binding_id'])
        if row.get('run_id'):
            await self.api.stop(row['run_id'])
        else:
            self.registry.update(row['task_id'], status='cancelled')
            self.capacity.release(row['task_id'])
        return self.registry.public(self.registry.get(row['task_id']))

    async def refresh(self, row):
        if not row.get('run_id') or row['status'] in TERMINAL: return row
        try:
            state = await self.api.request('GET', 'v1/runs/' + row['run_id'])
            current = self.registry.get(row['task_id'])
            if not current: return {**row, 'status': 'cancelled'}
            if current['status'] == 'stopping' and state['status'] not in TERMINAL:
                state = {**state, 'status': 'stopping'}
            fields = {'status': state['status'], 'approval': state.get('approval') if state['status'] == 'waiting_for_approval' else None}
            if state['status'] in TERMINAL:
                fields.update(message=str(state.get('output') or state.get('error') or current.get('stop_reason') or state['status'])[:12000],
                    pending_steer=state.get('pending_steer'), completed_at=time.time())
                self.store.revoke(row['binding_id'])
                self.capacity.release(row['task_id'])
            return self.registry.update(row['task_id'], **fields)
        except Exception:
            # Keep the reservation and last known state, but never call stale data authoritative.
            return {**row, 'status': 'unavailable', 'message': 'Native task status unavailable; do not resubmit or assume completion.'}

    async def control(self, body):
        if body.get('action') == 'owner_status':
            rows = self.registry.rows()
            active = [r for r in rows if r['status'] not in TERMINAL]
            recent = [r for r in rows if r['status'] in TERMINAL][-5:]
            result = [self.registry.public(await self.refresh(r)) for r in recent + active]
            for row in result: row['message'] = row.get('message', '')[:900]
            return {'status':'ok', 'tasks':result}
        if body.get('action') == 'owner_approve':
            if body.get('choice') not in {'once', 'deny'}: raise ValueError('invalid approval')
            for row in self.registry.rows():
                if row['status'] in TERMINAL: continue
                current = await self.refresh(row)
                if (current.get('approval') or {}).get('request_id') == body.get('request_id'):
                    return await self.api.request('POST', 'v1/runs/' + row['run_id'] + '/approval',
                        json={'request_id': body['request_id'], 'choice': body['choice']})
            raise ValueError('no matching approval')
        if body.get('action') == 'recall_authority':
            binding = self.store.run_binding(body['run_id'])
            row = self.registry.authority(body['run_id'])
            if not row or binding['binding_id'] != body['binding_id']:
                raise PermissionError('no matching active run')
            return {'conversation_id': row['conversation_id'], 'profile': binding['profile'], 'caller_id': binding['caller_id']}
        _, _ = self.owned(body['binding_id'], body['conversation_id'])
        rows = [r for r in self.registry.rows() if r['conversation_id'] == body['conversation_id']]
        action = body.get('action', 'status')
        if action == 'status':
            if body.get('task_id'):
                rows = [r for r in rows if r['task_id'] == body['task_id']]
                if not rows: raise PermissionError('unknown caller task')
            else:
                pending = [r for r in rows if r['status'] not in TERMINAL]
                rows = [r for r in rows if r['status'] in TERMINAL][-max(0, 8-len(pending)):] + pending
            result = [self.registry.public(await self.refresh(r)) for r in rows]
            for row in result:
                budget = 6500 if body.get('task_id') else 600
                message = row.get('message', '')
                row['message'] = message.encode('utf-8')[:budget].decode('utf-8', errors='ignore')
                row['result_truncated'] = row['message'] != message
            return {'status': 'ok', 'tasks': result}
        target = next((r for r in rows if r['task_id'] == body.get('task_id')), None)
        active = [r for r in rows if r['status'] not in TERMINAL]
        if not target and not body.get('task_id') and len(active) == 1: target = active[0]
        if not target: raise ValueError('Select a task; no unique active task matches.')
        if action == 'cancel': return await self.stop(target)
        if action == 'continue':
            binding = self.store.binding(body['binding_id'])
            self.continuation_allowed(binding)
            if target['status'] in TERMINAL or target['status'] == 'stopping': raise ValueError('task is no longer running')
            if not target['continue_after_call']:
                target = self.registry.update(target['task_id'], continue_after_call=True, delivery='pending',
                    expires_at=time.time()+self.bridge.config.background_task_minutes*60)
            return {**self.registry.public(target), 'message': 'Authorized to continue after hangup until the stated expiry. Results will be sent through native Telegram.'}
        if action == 'steer':
            text = body.get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > 8000: raise ValueError('invalid correction')
            result = await self.api.steer(target['run_id'], text)
            return {'status': 'pending' if result.get('accepted') else 'error', 'task_id': target['task_id'],
                'message': 'Correction accepted for delivery, not yet confirmed applied.' if result.get('accepted') else 'Correction not accepted.'}
        if action == 'approve':
            if body.get('choice') not in {'once', 'deny'}: raise ValueError('invalid approval')
            return await self.api.request('POST', 'v1/runs/' + target['run_id'] + '/approval',
                json={'request_id': body['request_id'], 'choice': body['choice']})
        raise ValueError('invalid task control')

    async def settle_conversation(self, conversation):
        deadline = time.monotonic()+8
        while True:
            rows = [r for r in self.registry.rows() if r['conversation_id'] == conversation and r['status'] not in TERMINAL]
            if not rows: return
            for row in rows: await self.refresh(row)
            if time.monotonic() >= deadline:
                raise ValueError('Native tasks are still stopping; retry deletion after they settle.')
            await asyncio.sleep(.2)

    async def cancel_conversation(self, conversation):
        for row in self.registry.rows():
            if row['conversation_id'] == conversation and row['status'] not in TERMINAL:
                await self.stop(row)

    async def drain_events(self, run_id):
        # Native SSE is a single-consumer stream; the gateway owns it, the call
        # reads durable task state. Losing this stream never cancels/replays work.
        try:
            async with self.api.http.stream('GET', f'v1/runs/{run_id}/events') as response:
                response.raise_for_status()
                async for _ in response.aiter_lines(): pass
        except Exception:
            pass

    async def watch(self):
        while True:
            for row in self.registry.rows():
                try:
                    if row['status'] not in TERMINAL:
                        try:
                            if row['continue_after_call']:
                                if row['expires_at'] <= time.time(): raise PermissionError('expired')
                            else: self.store.binding(row['parent_binding'])
                        except PermissionError:
                            await self.stop(row, 'The bounded run authorization expired.' if row['continue_after_call'] else 'The call ended or its authority expired.')
                        if row.get('run_id') and row['run_id'] not in self.streams:
                            self.streams[row['run_id']] = self.spawn(self.drain_events(row['run_id']))
                        row = await self.refresh(self.registry.get(row['task_id']))
                    if row['status'] in TERMINAL and row['continue_after_call'] and row['delivery'] == 'pending':
                        # Claim durably before external I/O. A crash or timeout is
                        # uncertain delivery, never permission to resend.
                        self.registry.update(row['task_id'], delivery='uncertain')
                        self.spawn(self.deliver(row))
                except Exception:
                    pass  # A failed status/stop attempt retains the reservation for reconciliation.
            await asyncio.sleep(1)

    async def deliver(self, row):
        def send():
            current = self.registry.get(row['task_id'])
            managed = self.store.managed(row['session_id'])
            if not current or current['delivery'] != 'uncertain' or not managed or not managed['active']:
                return {}
            from tools.send_message_tool import send_message_tool
            with self.adapter._profile_scope(self.bridge.profile):
                # Output is plain result text. Requested attachments are sent by
                # Hermes's native hermes send command during the task itself.
                message = f"{row['label']} [{row['status']}]\n{row.get('message', '')}"
                message = message.replace('MEDIA:', 'Media path:').replace('[[as_document]]', '')[:3800]
                return json.loads(send_message_tool({'action': 'send', 'target': 'telegram', 'message': message}))
        try:
            result = await asyncio.to_thread(send)
            if result.get('success'):
                self.registry.update(row['task_id'], delivery='sent')
        except Exception:
            pass  # No custom transport, test message or automatic second send.
