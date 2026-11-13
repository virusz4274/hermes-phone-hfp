# Hermes catalog compatibility audit — 2026-10-21

The current integration has substantial architectural dependencies that a catalog
migration must preserve. Treating this as manifest cleanup and credential getter
replacement understates the work. This document records 20 affected areas,
reproduced failures, existing behavior, and the evidence still needed before a
submission or an implementation estimate.

The earlier assessment also overstated one issue: private imports are warnings,
not automatic rejection. The current core-override lint found no overrides in
either the existing wrapper or the temporary layout containing its implementation.
That result does not establish that every private interface is supported.

## Evidence and limits

- Phone repository: `8b8d072645f5cc8930d6a14f6793b682273e56dd`; tracked implementation
  was clean when examined. Existing untracked video/script assets were not changed.
- Installed Hermes: clean checkout `130b8f2c5dbca93a81aa396dd2ba44420d78f6f0`.
- Current upstream static checker source:
  `4d04303818b61bf3aa09278af48356315515965b`.
- The focused suite passed **299 tests**, with one Starlette/httpx deprecation
  warning. It covered bridge HTTP, native authority, routing, owner lookup, tasks,
  workflows, delivery, memory, installation, conversations, callback rescheduling,
  reminders, callback policy, HTTP control, API clients, and voice backends.
- `scripts/check_hermes.py` passed against installed Hermes in a disposable home.
  It exercised real plugin/session/cron interfaces without a provider call,
  telephone call, or message delivery.
- Additional probes used synthetic credentials and temporary homes/databases.
  They did not restart services, change real profiles, or contact a model.
- The two current static linters were fetched from the exact upstream commit
  above and run in a temporary process using the installed checkout's core-module
  inventory. This was **not** a full run of current Hermes catalog CI.
- The current wrapper produced zero private-import findings because its actual
  implementation is outside the plugin directory. A temporary diagnostic copy
  placing `hfp_mcp` inside that directory produced **41 import-site findings across
  12 Hermes modules**, and zero core-override findings. This temporary layout was
  a visibility probe, not a selected packaging design.

These are different evidence levels: **reproduced** means an isolated probe
demonstrated the behavior; **confirmed dependency** means the implementation
explicitly relies on it; **migration risk** means a proposed replacement needs
verification. Migration risks below are not assertions of an existing exploit or
guaranteed catalog refusal. This is an audit of the current code and its catalog
transition, not a proof that every hardware/runtime combination is safe.

## Credentials, profiles, and routing

### 1. Several kinds of credentials have different owners

**Confirmed policy conflict and reproduced scope mismatch.**
`Endpoint.secret()` uses `settings.service_environment()`, which reads the daemon
environment file and overlays process environment. `phone_cli.control()` reads
the daemon bearer token directly from a file unless an explicit token exists.
The bridge uses `get_secret()` for inbound authentication, with an environment
fallback if the import is unavailable.

An isolated probe installed a guest secret scope while the process held a
different synthetic value: Hermes returned the guest value, but `Endpoint.secret()`
returned the process value. A mechanical conversion to `get_secret()` would change
existing cross-endpoint behavior.

Names also differ across components: the example daemon uses
`HFP_HERMES_API_KEY` / `HFP_GUEST_API_KEY`, while each receiving Hermes profile
configures its own `API_SERVER_KEY`. Reading `get_secret(endpoint.token_env)`
without mapping these roles can fail even when the correct profile key exists.

The inventory includes Hermes API credentials, bridge credentials, daemon control
credentials, the daemon's Gemini API key, a caller-identity HMAC key, and bounded
memory-closeout grants. They cannot all be treated as interchangeable provider
keys. In particular, a local pseudonymization key is not automatically a vendor
login credential.

Required preservation: explicit provisioning and rotation between the daemon
and each receiving profile; profile-local resolution inside the plugin; continued
support for the daemon's independently configured voice connection. Hermes itself
may read its profile `.env`; the prohibited plugin behavior is directly reading
credential files. A companion daemon does not excuse that behavior in plugin code.

Evidence: `src/hfp_mcp/routing.py:25`, `settings.py:73`, `settings.py:97`,
`phone_cli.py:16`, `hermes_bridge.py:330`, `hermes_api.py:20`,
`gemini_live.py:85`, `caller_context.py:24`, `call_memory.py:105`.

### 2. HTTP requests, startup callbacks, and background tasks need different scoping

**Confirmed dependency; migration risk.** Named-profile custom routes are
explicitly mirrored by the default bridge. Workers for task observation, memory
closeout, and workflow recovery start from aiohttp startup callbacks. Request
scoping does not by itself demonstrate correct scoping of a later startup callback
or an independently spawned worker. The task observer constructs a profile API
client during startup, currently using the shared environment lookup.

Required preservation: each request and worker must keep its owning profile,
including after hangup, during recovery, and through `asyncio.to_thread`. Test
personal and guest profiles concurrently with different credentials. Do not ask
guest profiles to carry owner secrets just to make a shared worker initialize.
An API key is cached in the client's Authorization header while bridge keys are
resolved per request, so credential rotation needs an explicit restart/refresh policy.

Evidence: `hermes_bridge.py:305`, `hermes_bridge.py:839`, `phone_tasks.py:161`,
`phone_tasks.py:544`, `call_memory.py:451`, `phone_workflows.py:788`,
`hermes_api.py:20`, `hermes_api.py:30`.

### 3. Central routing cannot be replaced by independent profile settings

**Confirmed dependency.** The daemon and bridges must interpret equivalent
routing configuration. The bridge re-derives the caller's endpoint and policy
instead of trusting the daemon's requested permission envelope. A change that
moves configuration into separate profile stores needs to preserve this agreement.

Bridges and the daemon controller retain startup snapshots, while workflow
authorization reloads the routing file. Preserve an explicit coordinated restart
or reload policy; a new per-profile settings UI must not leave the components
evaluating different route revisions.

Incoming and outgoing unknown numbers also differ. With no default, an unknown
incoming caller is declined, while an owner-requested outgoing destination can
receive the reserved conversation/notes-only fallback. Configured matching routes,
including a guest default, still take precedence over the outgoing fallback.

Required preservation: exact known-number matching, blocked-number precedence,
withheld-number handling, guest default behavior, explicit outbound endpoint
selection, and conversation-only outbound permissions. There is no need for one
credential or Hermes profile per unknown caller.

Evidence: `setup/phone.example.yaml`, `routing.py:178`, `routing.py:235`,
`routing.py:245`, `hermes_bridge.py:375`, `phone_controller.py:225`.

### 4. Purposeful calls already have a cross-profile limitation

**Reproduced existing limitation.** Scheduling a call objective from the personal
profile to a number explicitly routed to a guest profile raises
`PermissionError: number_match`. `PhoneWorkflows.authorize()` requires the
recipient's resolved endpoint to belong to the workflow's owning profile.
The failure occurred before creating any native cron job or dialing.

A raw call and a purpose-driven native workflow take different paths. Preserving
number routing alone therefore does not establish that all owner call controls
can operate across profiles. `owner_access` is a lookup permission and must not be
interpreted as permission to schedule or act in the other profile.

Required decision: either document this existing restriction or design explicit
cross-profile workflow authority, schedule ownership, caller memory ownership,
and result delivery. Credential sharing alone cannot resolve the ownership issue.

Evidence: `hermes_bridge.py:935`, `phone_workflows.py:358`,
`phone_workflows.py:378`, `phone_workflows.py:496`.

### 5. Owner lookup has a two-sided authorization protocol

**Confirmed dependency.** An owner can read explicitly permitted records in
another profile. The target gateway validates source authority by calling the
source gateway; authority is checked again around the read. The daemon combines
profile notes with its own call/transcript ledger and records an audit receipt.

Required preservation: `owner_access`, caller/run revocation, number-to-profile
ownership, pagination, transcript-retention controls, and source verification.
Making every profile resolve only its own secrets removes the current ability to
authenticate some peer calls unless this protocol is redesigned. Simply copying
owner credentials into the guest profile broadens its authority.

Evidence: `owner_lookup.py:121`, `hermes_bridge.py:729`,
`hermes_bridge.py:1042`, `http_routes.py:130`, `tests/test_owner_lookup.py`.

## Initialization and caller execution

### 6. A fresh install can fail twice, at different lifecycle stages

**Reproduced.** Registration in an empty Hermes home raises `FileNotFoundError`
for `config.yaml`. Supplying an empty config makes registration succeed with
15 tools and two hooks. However, wiring the plugin with no phone endpoints then
running aiohttp startup raises `RuntimeError: coroutine raised StopIteration`:
`GatewayTasks.start()` assumes an endpoint exists for the current profile.

The custom installer normally supplies configuration that masks these assumptions.
A catalog install must handle an unconfigured plugin without breaking gateway
startup. A registration-only fix is insufficient. Missing optional Gemini
credentials must also not disable a classic-only installation; declaring every
endpoint's credentials as mandatory would couple unrelated profiles at load time.

Evidence: `hermes_bridge.py:824`, `hermes_bridge.py:834`,
`phone_tasks.py:161`, `setup/install_hermes.py:14`,
`hermes_phone_plugin/plugin.yaml`.

### 7. Caller authorization depends on HTTP admission timing and exact run identity

**Confirmed dependency.** A custom HTTP middleware surrounds native `/v1/runs`
admission and persists the run-to-binding association before execution can use it.
Tool hooks consult the current native run identity, caller lease, and session
lineage. A follow-up decision gets its own tightly restricted run.

Required preservation: a revoked call cannot be revived by resuming its session,
an idempotent retry cannot acquire a new caller lease, and late/compaction-child
execution must remain associated with the original authority. Removing the
middleware or replacing run identity with just a session ID would remove a
security boundary. A public replacement must provide equivalent ordering and
identity guarantees. The aiohttp middleware itself was not flagged as a core override.

Evidence: `hermes_bridge.py:120`, `hermes_bridge.py:140`,
`hermes_bridge.py:150`, `hermes_bridge.py:604`,
`phone_tasks.py:277`, `tests/test_native_phone_authority.py`.

### 8. Guest isolation spans tools, memory, and integration registration

**Confirmed dependency.** Restricted profiles disable shared/user memory and
unscoped external memory, constrain API toolsets, and disallow raw MCP servers.
Caller-aware integrations register guarded wrappers that receive a trusted
binding. Phone control handlers independently check policy as well as using the
pre-tool hook. Caller ID chooses routing but does not authenticate a human.

Required preservation: guest callers must not inherit personal memory or owner
controls through a convenient shared profile. Keep lease expiry, forgotten caller
state, caller-scoped note revisions, guarded external integrations, and normal
Hermes approvals. Test real dispatch and deferred tool calls; declaring tool names
in a manifest does not enforce these permissions.

Evidence: `hermes_bridge.py:33`, `hermes_bridge.py:108`,
`hermes_bridge.py:277`, `hermes_bridge.py:1107`, `SECURITY.md`.

## Packaging, scheduling, and delivery

### 9. A catalog pin currently identifies a wrapper, not its installed implementation

**Confirmed dependency.** `hermes_phone_plugin/__init__.py` imports the global
`hfp_mcp` package. The custom installer installs the root project into the selected
Hermes interpreter. The wrapper declares no Python requirements; the wheel ships
`hfp_mcp` but not the plugin directory. Root metadata outside a catalog `subdir`
does not supply adjacent plugin dependency metadata.

Required preservation: a clean install must obtain the reviewed implementation;
Hermes environment replacement must retain its declaration; multiple profiles
must not claim different reviewed implementations while importing one global
package version. Newly declared native/daemon extras must not enter Hermes's
shared dependency union: the daemon requires MCP 1.x while installed Hermes pins
MCP 2. Packaging also changes what the scanner can inspect. A zero-warning wrapper
scan is not evidence about the separately installed implementation.

Evidence: `hermes_phone_plugin/__init__.py`, `pyproject.toml:19`,
`pyproject.toml:29`, `pyproject.toml:68`, `setup/install_hermes.py:218`.

### 10. Callback workers run outside the plugin's immediate import context

**Confirmed dependency.** Generated files under the profile's `scripts/` import
`hfp_mcp.phone_workflow_runner`. Workers inspect native execution identities and
cron execution rows, authenticate back to the gateway, and require a script budget
greater than 150 seconds. Their dependencies must exist in the actual cron worker
interpreter, not merely in the plugin loader.

Required preservation: a newly self-contained plugin cannot strand already
scheduled scripts by renaming/moving the package. Test jobs across environment
replacement, restart, plugin update/disable, gateway outage, and timeout. The
documented `ctx.current_cron_execution()` surface must be evaluated for the
separate no-agent worker path; availability inside a plugin tool handler alone
does not establish equivalence.

Evidence: `phone_workflows.py:242`, `phone_workflows.py:273`,
`phone_workflows.py:297`, `phone_workflow_runner.py:40`,
`phone_workflow_runner.py:82`, `phone_workflow_runner.py:99`.

### 11. Scheduler replacements must retain atomicity and uncertainty handling

**Confirmed dependency.** Callbacks, reports, and notifications use native cron
jobs, firing fences, live claims, and read-back verification. A reschedule may
change linked call and notification jobs. Persisted request IDs, revisions,
execution IDs, evidence, and decision state distinguish an accepted schedule
from an actual completed call.

Required preservation: a callback cannot fire at the old time during a move;
cancel/retry cannot duplicate a call; a crash after an external write must not
become automatic permission to repeat it. Replacing private cron calls with a
sequence of create/update commands or a daemon timer is not equivalent. Establish
public atomic job operations or obtain upstream support before removing fences.

Evidence: `phone_workflows.py:140`, `phone_workflows.py:267`,
`phone_workflows.py:335`, `phone_workflows.py:519`,
`reminders.py:119`, `tests/test_callback_rescheduling.py`.

### 12. Result delivery is profile-scoped and separate from execution authority

**Confirmed dependency.** Delivery resolves Hermes's default configured gateway
destination rather than the phone/API origin. Saved destinations retain their
recipient identity. Call-only reminders suppress gateway messages. Continued
tasks can outlive a call only with bounded admin authorization; a durable delivery
claim prevents uncertain sends from being repeated.

Required preservation: no accidental message to the wrong profile, no broadcast,
no duplicate notification after recovery, and no call-only reminder becoming a
Telegram message. The plugin directly imports messaging and delivery internals.
A supported tool-dispatch replacement needs the same profile, approval behavior,
recipient selection, and receipts. Continuing a task is not blanket permission
to send arbitrary external results.

Evidence: `hermes_compat.py:9`, `phone_tasks.py:194`,
`phone_tasks.py:518`, `phone_tasks.py:544`, `phone_workflows.py:660`,
`tests/test_gateway_reminder_delivery.py`.

## Voice, memory, and durable state

### 13. Moving call memory to the public LLM facade changes task routing

**Confirmed dependency and reproduced migration failure.** Automatic note
extraction calls `agent.auxiliary_client.async_call_llm(task='phone_memory')`
inside a private profile scope, with no tools and bounded time/token budgets.
The plugin does not register an auxiliary task. An isolated trust-gate probe
rejected the same unregistered task through the facade's task policy with
`PluginLlmTrustError` before any provider call.

Required preservation: register/own the intended auxiliary slot and migrate its
configuration deliberately; preserve the receiving profile, tools-disabled
extraction, JSON validation, revision-safe merging, and recovery limits.
Closeout grants intentionally survive ordinary call-binding revocation for a
bounded period; forgetting a caller must still invalidate pending extraction.
Changing the LLM call alone does not address those ownership semantics.

Evidence: `call_memory.py:65`, `call_memory.py:105`, `call_memory.py:135`,
`call_memory.py:451`, `hermes_bridge.py:824`.

### 14. Classic speech and Gemini Live have different runtime boundaries

**Confirmed dependency.** Classic voice calls Hermes transcription/TTS tools
directly, creates temporary audio, and uses ffmpeg to return mono signed 16-bit
8 kHz PCM. Gemini Live runs in the Bluetooth daemon with its own SDK connection
and key. One provider change cannot be assumed to migrate both paths.

Required preservation: correct profile/provider credentials for classic speech,
8 kHz format, response limits, bounded conversion, interruption/cancellation,
and explicit fallback behavior. Admitting the plugin should not require a live
phone, ffmpeg invocation, provider sign-in, or Gemini key during registration.
Provider success and voice quality still need live acceptance; unit tests do not
establish them.

Evidence: `speech.py`, `voice_backends.py:39`, `gemini_live.py:85`,
`hermes_bridge.py:517`, `docs/compatibility.md:13`.

### 15. A storage migration affects identity and native session discovery

**Reproduced identity issue; confirmed path coupling.** `CallerStore` derives
caller IDs from a private adjacent key. Copying `callers.sqlite3` to a new location
without its `.key` created different caller IDs and made a saved synthetic note
unreachable by the current identity. The native session adapter also derives
`state.db` from `store.path.parent.parent`; moving the caller store to a standard
plugin-data directory would change which native database it opens.

Required preservation: migrate the identity key with caller data and preserve
note revisions, forget generations, run bindings, workflow state, and daemon
conversation/transcript links. Locate native Hermes storage independently of
plugin data. Use SQLite-safe migration with stopped writers or a coordinated
backup, including outstanding WAL state. Renaming a profile or route must not
silently grant access to another caller's notes.

Evidence: `caller_context.py:24`, `caller_context.py:66`,
`hermes_sessions.py:8`, `conversations.py:37`, `docs/maintenance.md`.

### 16. Task capacity is coordinated through a shared daemon database

**Reproduced migration risk.** `TaskCapacity` opens the daemon ledger from inside
the plugin and stores reservations shared by all profiles/gateways. With one
shared database and a limit of one, the second synthetic reservation was refused.
With separate per-profile databases, both reservations succeeded.

Required preservation: moving each profile to its own plugin database must not
multiply the configured concurrency limit. Choose a shared authority for capacity
and recovery, and test independent gateway processes. Remote Hermes hosts cannot
be assumed to share the Bluetooth host's SQLite file. Reading daemon configuration
and opening its ledger also need disclosure and a supported ownership boundary.

Evidence: `phone_tasks.py:24`, `phone_tasks.py:143`,
`phone_tasks.py:241`, `hermes_bridge.py:315`.

### 17. Compaction is a separate compatibility problem from ordinary session access

**Confirmed dependency.** `hermes_state.SessionDB` is on the current lint's public
allowlist; importing it is not automatically forbidden. Compaction additionally
uses private adapter/agent construction, persistence markers, and compression
methods, and already rejects one backend mode. Session continuation and deletion
traverse a persisted lineage.

Required preservation: the compressor must keep durable history and run ownership
consistent. Do not re-create a generic agent and assume it shares the original
backend's state, or discard session continuity just to remove an import warning.
Evaluate a supported compaction operation separately, including sparse history,
rotated sessions, active tasks, and backend-specific state.

Evidence: `hermes_sessions.py:8`, `hermes_sessions.py:32`,
`hermes_sessions.py:49`, `hermes_sessions.py:62`, `hermes_compat.py:65`.

## Installation, updates, and admission evidence

### 18. Hot enablement and disablement do not match the custom installer lifecycle

**Reproduced.** Calling `PhoneBridge.wire()` on a frozen aiohttp application raises
`RuntimeError: Cannot modify frozen list` while adding startup callbacks. Route
registration also requires an unfrozen router. A successful plugin registration
or catalog enable is therefore not proof that phone endpoints became operational.

Hermes documents retained wired handlers until gateway restart after disablement.
The phone plugin additionally keeps global bridge/capability registries and owns
background observers. Cleanup cancels workers, but the source does not show a
full global-registry/CallerStore/TaskCapacity teardown for reloads.

Required preservation: explicitly report restart requirements; test enable/update
while running, disable during a call, pending schedules, and restart recovery.
Disabling a plugin must not be represented as revoking all previously scheduled
telephone work unless that effect is implemented and verified. The public
platform-handler registration API is itself valid and need not be removed.

Evidence: `hermes_bridge.py:26`, `hermes_bridge.py:305`,
`hermes_bridge.py:837`, `phone_tasks.py:175`, `phone_workflows.py:912`,
`call_memory.py:454`, `setup/install_hermes.py:119`.

### 19. The hardware daemon and plugin have a coordinated release lifecycle

**Confirmed dependency.** The hardware installer installs OS/native dependencies,
changes Bluetooth/D-Bus/WirePlumber configuration, creates a separate environment,
and enables/restarts services. Its systemd unit runs from the repository's venv.
The Hermes installer separately installs the base package and enables the plugin.
Existing documentation requires daemon and plugin updates together for some API
and persistence changes. The bridge advertises version `1` plus capability flags,
not a complete two-component compatibility matrix.

Required preservation: a catalog plugin update must detect an incompatible daemon
and guide an explicit coordinated upgrade without silently changing host services
or replaying calls. Provide clean-install, legacy-install adoption, rollback,
manual daemon removal, and scheduled-worker handling. Test device-host and
Hermes-host roles separately. Native dependency extras cannot be merged into the
Hermes interpreter as an installation shortcut.

These manually invoked installers are not evidence of a prohibited autonomous
self-updater. Their privileges, editable runtime, selected revision, and external
services still need accurate disclosure.

Evidence: `setup/install.sh:50`, `setup/install.sh:89`,
`setup/install_hermes.py:198`, `setup/hfp-mcp.service:17`,
`hermes_api.py:39`, `docs/maintenance.md`, `docs/compatibility.md:17`.

### 20. Admission success and existing test success cover different things

**Confirmed gaps.** The wrapper manifest lacks tool/hook inventories and dependency
declarations. Its source folder has no README for the default subdirectory listing
renderer. The existing CI tests the phone package and its wheels, but does not
run current catalog validation or the real-Hermes compatibility script. The
compatibility script pre-creates configuration, constructs a manifest in code,
and invokes selected handlers rather than starting a complete multiplexed gateway.
Those choices explain why it passes while the empty-config startup probe fails.

The installed validator lacks the current private-import/core-override checks.
Compatibility documentation and the source constant also name different tested
Hermes commits; neither is a verified minimum released SemVer version. Missing
`requires_hermes` is not itself an automatic validator failure, but a truthful
support floor remains necessary for users to assess this integration.

Required evidence: clean clone at the submitted SHA, declared dependencies under
Hermes constraints, real manifest discovery, complete gateway startup, multiplexed
personal/guest behavior, and a release-based compatibility matrix. Keep declared
tools/hooks distinct from privileged capability grants. Passing an AST lint
neither proves runtime compatibility nor justifies requiring a rewrite of every
private call.

Evidence: `hermes_phone_plugin/plugin.yaml`, `.github/workflows/ci.yml`,
`scripts/check_hermes.py:40`, `scripts/check_hermes.py:77`,
`scripts/check_hermes.py:99`, `hermes_compat.py:32`,
`docs/compatibility.md:9`, `docs/validation.md`.

## Reproduced results

| Probe | Observed result | Meaning |
| --- | --- | --- |
| Empty Hermes home registration | `FileNotFoundError` for `config.yaml` | Current bootstrap fails admission's isolated registration shape. |
| Registration with `{}` configuration | 15 tools, `pre_tool_call`, `pre_llm_call` | Inventories can be measured after bootstrap succeeds. |
| Gateway startup with no phone endpoints | `RuntimeError: coroutine raised StopIteration` | Registration tolerance alone would not fix startup. |
| Late wiring into frozen aiohttp app | `RuntimeError: Cannot modify frozen list` | Running-gateway enablement needs an explicit lifecycle strategy. |
| Guest secret scope versus process environment | Hermes returned the guest value; endpoint lookup returned the process value | Existing lookup does not follow profile isolation. |
| Personal objective to a guest-routed destination | `PermissionError: number_match` | Existing cross-profile objective limitation, before dialing. |
| Unknown incoming/outgoing without a guest default | `unmapped` / `outbound_notes` | Incoming fallback and owner-initiated outbound behavior differ. |
| Database copied without adjacent identity key | Caller identity changed; note lookup returned empty | Data-directory migration must preserve identity material. |
| Shared capacity database, limit 1 | First reservation true, second false | Shared limit works across owners. |
| Separate capacity databases, limit 1 | Both reservations true | Per-profile storage alone loses the aggregate limit. |
| Unregistered `phone_memory` through LLM task policy | `PluginLlmTrustError` | A public-facade migration needs task ownership/configuration. |
| Current core-override lint, wrapper and diagnostic full layout | No findings | No automatic core-override rejection was established. |
| Current private-import lint | Wrapper: 0; diagnostic full layout: 41 | The wrapper hides substantial coupling from directory-only checks. |

The focused test command was:

```bash
.venv/bin/python -m pytest -q \
  tests/test_hermes_bridge_http.py tests/test_native_phone_authority.py \
  tests/test_phone_routing.py tests/test_owner_lookup.py tests/test_phone_tasks.py \
  tests/test_phone_workflows.py tests/test_gateway_reminder_delivery.py \
  tests/test_call_memory.py tests/test_hermes_plugin_install.py \
  tests/test_installer_env.py tests/test_phone_conversations.py \
  tests/test_callback_rescheduling.py tests/test_reminders.py \
  tests/test_callback_policy.py tests/test_http_control.py \
  tests/test_hermes_api.py tests/test_voice_backends.py
```

The real-Hermes check used the installed Hermes interpreter:

```bash
<hermes-python> scripts/check_hermes.py --hermes-root <hermes-source-checkout>
```

## Acceptance evidence still required

Before estimating implementation or claiming readiness, validate a candidate
design against these scenarios:

1. A clean catalog install before daemon setup; plugin discovery and gateway
   startup remain usable while configuration is incomplete.
2. Owner, known caller, unknown caller, withheld caller, and blocked caller under
   standalone and multiplexed profiles, with different synthetic profile secrets.
3. Unknown outgoing numbers with and without a guest default, plus purposeful
   calls to numbers explicitly routed to another profile.
4. Owner lookup across permitted profiles, denied scope, mid-read revocation,
   multiple configured endpoints, and unavailable source gateway.
5. Actual native tool dispatch, deferred dispatch, continuation after compaction,
   denied approval, call hangup, task expiry, and attempted stale-session reuse.
6. Linked callback/notification create, move, cancel, simultaneous fire, restart
   during admission, uncertain dialing, and uncertain message delivery.
7. Cron workers across managed-environment replacement and plugin update;
   background execution always uses the owning profile and bounded authority.
8. Memory extraction using a declared public task, concurrent note revision,
   caller forget during extraction, and interrupted closeout recovery.
9. Storage migration including identity keys, SQLite state, native session paths,
   old scheduled scripts, cross-profile capacity, and reverse/rollback behavior.
10. Hot enablement, disablement with active work, clean restart, mismatched daemon
    and plugin releases, and Linux hardware/provider acceptance for both voices.

None of these scenarios requires giving unknown callers credentials or removing
guest support. They require preserving the current authority boundaries while
changing installation and integration mechanisms.

## Sources and admission interpretation

The catalog requires reviewed exact commits, accurate declarations, permitted
credential access, respected approvals, disclosed external behavior, dependency
compatibility, and local validation. See the [submission rules](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission).

The [plugin guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
documents managed dependencies, durable data helpers, public native platform
handlers, and activation behavior. These are possible migration building blocks;
their existence does not establish equivalence with this plugin's contracts.

The [multiplexing design](https://hermes-agent.nousresearch.com/docs/developer-guide/multiplexing-gateway)
describes isolation of profile state and secrets. The [LLM facade](https://hermes-agent.nousresearch.com/docs/developer-guide/plugin-llm-access)
documents plugin-owned auxiliary task routing and host-owned credentials.

Static results used the pinned [core-override checker](https://github.com/NousResearch/hermes-agent/blob/4d04303818b61bf3aa09278af48356315515965b/hermes_cli/plugin_validate_core_override.py)
and [private-import checker](https://github.com/NousResearch/hermes-agent/blob/4d04303818b61bf3aa09278af48356315515965b/hermes_cli/plugin_validate_private_imports.py).
The latter explicitly reports warnings; it does not refuse a plugin merely for
an import. Scanner results are evidence for review, not approval or a substitute
for the end-to-end checks above.
