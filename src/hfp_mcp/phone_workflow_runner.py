"""Native Hermes no-agent phone script. Stdout is delivered by Hermes only."""
import asyncio
import os
import time
from .hermes_api import HermesAPI
from .routing import RoutingConfig


def main(callback_id, endpoint_name):
    asyncio.run(execute(callback_id, endpoint_name))


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
    asyncio.run(report_only(callback_id, endpoint_name, attempt_id))


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
