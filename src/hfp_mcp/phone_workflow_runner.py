"""Native Hermes no-agent phone script. Stdout is delivered by Hermes only."""
import asyncio
import os
import time
from .hermes_api import HermesAPI
from .routing import RoutingConfig


def main(callback_id, endpoint_name):
    run_script(execute(callback_id, endpoint_name), callback_id, endpoint_name)


def run_script(operation, callback_id, endpoint_name):
    try:
        asyncio.run(operation)
    except Exception as exc:
        # Native cron delivers stdout verbatim. Retain diagnostics privately and
        # publish an accurate short message rather than an exception traceback.
        message_enabled = True
        try:
            from hermes_constants import get_hermes_home
            from pathlib import Path
            from .phone_workflows import WorkflowStore
            config = RoutingConfig.load()
            store = WorkflowStore(Path(get_hermes_home()) / 'hfp-phone/workflows.sqlite3', config.endpoints[endpoint_name].profile)
            try:
                with store.lock, store.db:
                    row = store.get(callback_id)
                    row['last_script_error'] = {'type': type(exc).__name__, 'detail': str(exc), 'at': time.time()}
                    store.save(row)
                    message_enabled = (row.get('delivery_plan') or {}).get('mode') != 'call'
            finally:
                store.close()
        except Exception:
            pass
        if message_enabled:
            print("I couldn't confirm the callback's status. Please check it before requesting another call.")


async def execute(callback_id, endpoint_name):
    execution_id = os.environ.get('_HERMES_CRON_EXTERNAL_WORKER')
    if not execution_id:
        raise PermissionError('phone scripts require a native Hermes execution')
    from cron.executions import get_execution
    execution = get_execution(execution_id)
    if not execution or execution.get('status') != 'running':
        raise PermissionError('native execution unavailable')
    from cron.scheduler_script import _get_script_timeout
    api = HermesAPI(RoutingConfig.load().endpoints[endpoint_name])
    script_budget = _get_script_timeout()
    if script_budget <= 150:
        await api.close()
        raise RuntimeError('native script timeout must exceed 150 seconds for bounded phone outcomes')
    observation_seconds = min(1800, script_budget - 150)
    deadline = time.monotonic() + observation_seconds + 120
    try:
        response = await api.request('POST', 'v1/hfp/workflows/' + callback_id + '/execute', bridge=True,
                                     json={'job_id': execution['job_id'], 'execution_id': execution_id, 'observation_seconds': observation_seconds})
        attempt_id = response['attempt_id']
        while time.monotonic() < deadline:
            result = await api.request('POST', 'v1/hfp/workflows/' + callback_id + '/report', bridge=True,
                                       json={'attempt_id': attempt_id, 'execution_id': execution_id})
            if result.get('ready'):
                if result.get('report'):
                    print(result['report'])
                return
            await asyncio.sleep(1)
        # The outcome is durable. A timeout must not invent a retry or duplicate notification.
        raise TimeoutError('phone observation/report is still pending; inspect the request')
    finally:
        await api.close()


def report_main(callback_id, endpoint_name, attempt_id):
    run_script(report_only(callback_id, endpoint_name, attempt_id), callback_id, endpoint_name)


def notification_main(callback_id, endpoint_name):
    run_script(notify(callback_id, endpoint_name), callback_id, endpoint_name)


async def notify(callback_id, endpoint_name):
    execution_id = os.environ.get('_HERMES_CRON_EXTERNAL_WORKER')
    if not execution_id:
        raise PermissionError('reminder requires native Hermes execution')
    from cron.executions import get_execution
    execution = get_execution(execution_id)
    if not execution or execution.get('status') != 'running':
        raise PermissionError('native reminder execution unavailable')
    api = HermesAPI(RoutingConfig.load().endpoints[endpoint_name])
    try:
        result = await api.request('POST', 'v1/hfp/workflows/' + callback_id + '/notification', bridge=True,
            json={'job_id': execution['job_id'], 'execution_id': execution_id})
        if result.get('ready') and result.get('report'):
            print(result['report'])
    finally:
        await api.close()


async def report_only(callback_id, endpoint_name, attempt_id):
    execution_id = os.environ.get('_HERMES_CRON_EXTERNAL_WORKER')
    if not execution_id:
        raise PermissionError('report requires native Hermes execution')
    api = HermesAPI(RoutingConfig.load().endpoints[endpoint_name])
    try:
        result = await api.request('POST', 'v1/hfp/workflows/' + callback_id + '/report', bridge=True,
                                   json={'attempt_id': attempt_id, 'execution_id': execution_id})
        if result.get('ready') and result.get('report'):
            print(result['report'])
    finally:
        await api.close()
