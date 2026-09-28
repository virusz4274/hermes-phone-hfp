"""Daemon-side memory capture, retries, and transcript-backed restart recovery."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import deque

import httpx

from .call_memory import BATCH_CHARS, TERMINAL


class MemoryCapture:
    def __init__(self, controller, data):
        self.controller, self.data = controller, data
        self.events = deque()
        self.context = deque(maxlen=4)
        self.wake = asyncio.Event()
        self.task = None
        self.ending = False
        self.api = None
        self.acknowledged = set(data.get('acknowledged', []))

    def persist(self):
        self.data['acknowledged'] = sorted(self.acknowledged)
        self.data['updated_at'] = time.time()
        ledger = self.controller.ledger
        if ledger:
            with ledger._lock, ledger._db:
                ledger._db.execute('INSERT INTO call_memory_runs VALUES (?,?) ON CONFLICT(call_id) DO UPDATE SET data=excluded.data',
                                   (self.data['call_id'], json.dumps(self.data)))
        current = self.controller.call_id
        previous = self.controller.status.get('memory_save', {}).get('call_id')
        if current == self.data['call_id'] or (current is None and previous in {None, self.data['call_id']}):
            self.controller.status['memory_save'] = self.data['result']

    def capture(self, event):
        if event['session_id'] != self.data['call_id'] or event['direction'] not in {'input', 'output'}:
            return
        if len(self.events) >= 512 or len(event['text']) > BATCH_CHARS:
            self.data['capture_complete'] = False
            self.data['result']['reason'] = 'capture_capacity_exceeded'
            self.persist()
            return
        self.events.append({k: event[k] for k in ('event_id', 'direction', 'text', 'timestamp')})
        self.data['captured_events'] += 1
        self.persist()
        self.wake.set()

    def start(self):
        self.task = asyncio.create_task(self.run(), name='call-memory-' + self.data['call_id'])
        return self.task

    def end(self):
        self.ending = True
        self.data['ended_at'] = time.time()
        self.persist()
        self.wake.set()

    async def run(self):
        endpoint = self.controller.config.endpoints[self.data['endpoint']]
        self.api = self.controller.api_factory(endpoint)
        attempts = 0
        try:
            while time.time() < self.data['receipt']['deadline']:
                try:
                    if self.events:
                        batch, size = [], 0
                        for event in self.events:
                            if (batch and size + len(event['text']) > BATCH_CHARS - 6000) or len(batch) == 96:
                                break
                            batch.append(event)
                            size += len(event['text'])
                        context = list(self.context)
                        while context and sum(len(e['text']) for e in context) + size > BATCH_CHARS:
                            context.pop(0)
                        await self.api.memory(self.data['receipt'], 'checkpoint', events=context + batch)
                        for event in batch:
                            self.events.popleft()
                            self.acknowledged.add(event['event_id'])
                            self.context.append(event)
                        self.persist()
                        attempts = 0
                    elif self.ending:
                        result = await self.api.memory(self.data['receipt'],
                            'status' if self.data.get('finalized') else 'finalize',
                            **({} if self.data.get('finalized') else {'capture_complete': self.data['capture_complete']}))
                        self.data['finalized'] = True
                        self.data['result'] = result
                        self.persist()
                        if result['status'] in TERMINAL:
                            return
                    else:
                        self.wake.clear()
                        await self.wake.wait()
                    if self.ending and not self.data.get("finalized"):
                        continue
                    if not self.ending or not self.events:
                        self.wake.clear()
                        try:
                            await asyncio.wait_for(self.wake.wait(), 15 if not self.ending else 2)
                        except TimeoutError:
                            pass
                except (httpx.HTTPError, ValueError, OSError, TimeoutError, sqlite3.Error) as exc:
                    attempts += 1
                    fatal = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403, 404}
                    self.data['result'] = {'call_id': self.data['call_id'], 'status': 'failed' if fatal else 'pending',
                                           'reason': 'closeout_rejected' if fatal else type(exc).__name__,
                                           'capture_complete': self.data['capture_complete']}
                    try:
                        self.persist()
                    except (OSError, sqlite3.Error):
                        pass
                    if fatal:
                        return
                    await asyncio.sleep(min(60, 2 ** min(attempts, 6)))
            self.data['result'].update(status='failed', reason='closeout_expired', capture_complete=self.data['capture_complete'])
            self.persist()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.data['result'].update(status='failed', reason=type(exc).__name__)
            try:
                self.persist()
            except Exception:
                import logging
                logging.getLogger(__name__).exception('Could not persist call memory failure')
        finally:
            self.events.clear()
            self.context.clear()
            await self.api.close()
            self.api = None


def initialize(ledger):
    if ledger:
        with ledger._lock, ledger._db:
            ledger._db.execute('CREATE TABLE IF NOT EXISTS call_memory_runs (call_id TEXT PRIMARY KEY, data TEXT NOT NULL)')


def new_capture(controller, receipt, *, retain_transcripts):
    if not receipt or not receipt.get('id'):
        result = receipt or {'status': 'failed', 'reason': 'closeout_unavailable', 'call_id': controller.call_id}
        controller.status['memory_save'] = result
        if controller.ledger:
            with controller.ledger._lock, controller.ledger._db:
                controller.ledger._db.execute('INSERT OR REPLACE INTO call_memory_runs VALUES (?,?)',
                    (controller.call_id, json.dumps({'call_id': controller.call_id, 'result': result, 'updated_at': time.time()})))
        return None
    capture = MemoryCapture(controller, {
        'call_id': controller.call_id, 'endpoint': controller.route.endpoint, 'receipt': receipt,
        'number': controller._number, 'outbound': bool(controller.outbound_context),
        'retain_transcripts': retain_transcripts, 'capture_complete': True, 'captured_events': 0,
        'ended_at': None, 'result': {k: v for k, v in receipt.items() if k not in {'id', 'token', 'deadline'}}})
    capture.persist()
    return capture


def recover(controller, *, transcripts_enabled):
    ledger = controller.ledger
    if not ledger:
        return []
    ledger.prune()
    with ledger._lock:
        rows = [json.loads(r[0]) for r in ledger._db.execute('SELECT data FROM call_memory_runs')]
    recovered = []
    for data in rows:
        if data['result']['status'] in TERMINAL:
            continue
        capture = MemoryCapture(controller, data)
        if data['endpoint'] not in controller.config.endpoints:
            data['result'].update(status='failed', reason='endpoint_removed')
            capture.persist()
            continue
        if time.time() >= data['receipt']['deadline']:
            data['result'].update(status='failed', reason='closeout_expired')
            capture.persist()
            continue
        route, _ = (controller.config.resolve_outbound(data['number']) if data.get('outbound') and data.get('number')
                    else controller.config.resolve(data.get('number')))
        continuity_enabled = bool(route and route.endpoint == data['endpoint'] and route.continuity)
        # A restart may have lost speech that never became a durable transcript.
        # Preserve that limitation even when every retained event is recovered.
        data['capture_complete'] = bool(data['ended_at'] and data['capture_complete'])
        if not data.get('finalized') and data['retain_transcripts'] and (transcripts_enabled or continuity_enabled):
            with ledger._lock:
                events = ledger._db.execute('SELECT event_id,direction,text,created_at FROM call_transcript WHERE call_id=? ORDER BY id',
                                             (data['call_id'],)).fetchall()
            for eid, direction, text, stamp in events:
                if eid and eid not in capture.acknowledged and direction in {'input', 'output'}:
                    if len(text) > BATCH_CHARS:
                        data['capture_complete'] = False
                        continue
                    capture.events.append(dict(event_id=eid, direction=direction, text=text, timestamp=stamp))
        if not data.get('finalized') and len(capture.acknowledged) + len(capture.events) < data['captured_events']:
            data['capture_complete'] = False
        capture.end()
        recovered.append(capture)
    return recovered
