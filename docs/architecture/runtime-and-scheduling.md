# Runtime and scheduling

Runtime lifecycle, display-step planning, playback execution and operating schedules.

Read the [architecture entry point and map](../architecture.md) first. Together,
the overview and linked subsystem documents form the architecture authority.

Catalog refresh is in [Composition and catalog](composition-and-catalog.md#manual-catalog-refresh-requests-52); rendering and panel targets are in [Rendering](rendering.md) and [Graphics and panel](graphics-and-panel.md).

## RuntimeHost lifecycle and cancellation

Issue #17 provides the packaged `postcardscene-runtime` executable and ordinary
Python `RuntimeHost`, independent of Flask and display hardware. #52 injects a
shared Database/PathPolicy and starts one catalog request thread; the executable
checks the explicitly migrated database before starting. A bare host remains
available for lifecycle tests. It starts no renderers or schedules. `host.status` returns a frozen
`RuntimeStatus` with lifecycle state and fixed safe summary; control callers can
inspect it and call `request_shutdown()`. No transport or serialization is added.

Lifecycle states are `starting`, `running`, `degraded`, `stopping`, `stopped`, and
`error`. A host runs once: normal execution advances from starting through running,
stopping and stopped. Host-thread `mark_degraded()` represents optional impairment
while remaining alive; fatal failures set error, request cancellation and propagate.
The executable returns nonzero on fatal failure without publishing raw exceptions.
Physical panel sleep/off remains separate: runtime stays running or degraded.

One host-owned `threading.Event` is the process cancellation boundary. SIGTERM and
SIGINT request the same shutdown path; the host waits interruptibly on that event.
Future source reconciliation, scheduling, provider refresh and renderer supervision
belong here, outside Flask requests, and must observe the shared event without
clearing it and bound their work and cleanup. This freezes ownership/cancellation,
not a general concurrency framework. #52 uses one dedicated catalog thread with
a bounded join; no job framework or third service is added.
Managed systemd installation remains owned by #11/#26.

## Scene/sequence runtime behavior

### Active playback configuration (#87)

Migration `0009_playback_settings` follows `0008_catalog_requests`, preserves
existing configuration/catalog rows, and adds two singleton settings:
`active_sequence_id = NULL` and `default_scene_dwell_seconds = 30`.
Apply it explicitly with database users stopped; startup never migrates.

The nullable active Sequence FK is the sole selection authority. `None` means
intentional safe idle, with no guessing from names, IDs, enabled state or order.
An existing disabled Sequence may be selected and remains temporarily ineligible
until re-enabled. Deleting the selected Sequence sets only the pointer to NULL
and deletes its owned memberships, preserving unrelated composition.

Ordinary Python callers use `settings.get_playback_settings`,
`set_active_sequence` and `set_default_scene_dwell`. Setters validate before
mutation in short write transactions; invalid input raises `ValueError`, while
schema/storage failures retain their separate database exceptions. Default dwell
is an integer 1–86400, never bool. No per-Widget dwell is introduced.

Progression duration precedence is membership `duration_override_seconds`, then
Scene `duration_seconds`, then application default dwell for image/portrait-pair
and web content. Untimed video uses natural EOF; explicitly timed video advances
at EOF or the dwell deadline, whichever occurs first. This normal progression
policy is distinct from #10 maximum-static-dwell/panel protection. #87 freezes
configuration semantics only; later #8 children execute timers and completion.

`playback_configuration.resolve_active_sequence(database, membership_id=...)`
returns frozen settings, Sequence and ordered membership records, optionally
including that membership's Scene and canonical Widget ID. One short read
transaction covers the entire result and closes before return. Eligibility
separates intentional idle, missing/disabled/damaged Sequence, missing/disabled
Scene, unsupported layout, and malformed occurrence/placement configuration.
A stale or foreign membership never selects another occurrence. Storage errors
propagate separately. Content-specific Widget/Source resolution stays with
#56/#71/#82; these snapshots neither select media nor authorize rendering.

V0 executes only `single` Scenes through #65's one active content surface.
A `portrait_image_pair` Widget in the `main` region satisfies side-by-side
portraits. Persisted `split_vertical` and `split_horizontal` remain valid,
round-trip configuration for V1 rich composition, but return unsupported-layout
eligibility in V0; they are never silently converted to single content.

Settings/configuration are reread at display-step boundaries, without DB
notifications or IPC. Restart creates a new transient epoch: ordered playback
later starts at the first currently eligible membership and shuffle creates a
fresh in-memory epoch. No cursor, history, random seed or current media identity
is persisted. #91 exposes active selection and dwell in authenticated Settings.

### Bounded display-step planner (#88)

`display_planner.DisplayPlanner(database, rng=...)` supplies serialized `next()`
(including automatic forward progression) and `previous()` planning calls.
`PlanResult` distinguishes ready, temporarily ineligible, history boundary and
configuration/selection failure without exposing raw exception values. Each new
forward call attempts one configured occurrence; #89 owns retries and backoff.
A successful new plan enters history when returned. The execution owner decides
when to request progression and how to handle a subsequent display failure.

Frozen `DisplayStep` records Sequence/membership/Scene/Widget identities, content
kind and exact canonical `(source_id, relative_path)` media identities only.
They carry neither file authority nor web targets. Execution must revalidate
current configuration/media before opening bytes or resolving a web target.
The planner calls no renderer and is not wired into RuntimeHost.

Ordered membership cycles preserve configured occurrences, including repeated
Scenes. Shuffle cycles visit every occurrence once and avoid repeating a cycle's
last occurrence immediately at the next cycle's start. Configuration is unchanged.
One transient media stream per Widget advances across all referencing occurrences.
`media_stream.select_candidate` uses canonical relative-path keysets and one wrap
for ordered selection. SQLite's existing Source/path uniqueness index supports
these queries without a temporary ordering tree; no schema/index is added.

Media shuffle uses an injected random fraction generated outside transactions,
then count plus ranked canonical selection in a short transaction. SQLite may
scan eligible index entries for counts/ranks; Python receives a single candidate
and at most 32 excluded paths, never a complete library. Up to 32 consumed
identities per Widget exclude recent repeats. Exhaustion relaxes oldest eligible
exclusions first, retaining the latest where alternatives exist. Small libraries
avoid reuse until exhausted; larger libraries do not promise a full permutation.

Portrait grouping uses #56's first/lookahead consumed-count contract. A rejected
lookahead remains a revalidated identity owned by the Widget stream; only consumed
candidates advance its cursor/recent window. A wrapped identical singleton is not
retained as its own lookahead.

History holds at most 32 steps, without URLs. Previous skips invalid entries
backward and stops at the oldest usable boundary. Next/automatic progression
replays valid forward history before planning new content. Replay rereads current
composition and media eligibility, requires exact historical pair members and
pair compatibility, and validates the current web target without retaining it.
Replay does not consume or rewind media streams. Source/Widget/Scene eligibility
changes may invalidate entries without resetting the epoch.

An observed active Sequence change, idle/disabled selection, membership identity
or order replacement, or mode change clears streams, cycles and history. A
concurrent epoch replacement detected during resolution returns ineligible and
starts the new epoch on the next call. Restart constructs empty transient state.
No database transaction spans RNG/history computation or future rendering.

### Playback execution worker (#89)

`runtime.playback.PlaybackWorker(database, presenter, stop_event)` owns one stdlib
thread and a maximum of 32 pending commands. `start()` is called once;
`submit(name, value=None)` returns a Future containing a fixed `Outcome`, including
busy/rejected/unavailable results. There is no executor pool or request-thread
rendering. The worker serializes planner calls, presentation, video snapshots,
transport and cleanup. #93 owns its construction in RuntimeHost and the concrete
V0 presenter; #58 remains the image prerequisite. No production renderer is
constructed by this worker capability.

`PlaybackState` consumes #88 Next/Previous directly. Planner `revalidate(step)`
reuses current composition/media validation without advancing order/history; it
supports re-presenting a held identity. Configuration is reread for each new or
replayed presentation. The presenter must re-resolve exact content authority at
execution, honor `start_paused` before video becomes audible, and implement bounded
cancellable operations. `clear` must retire all content/audio to safe black and
`stop` must finish retirement; cleanup uncertainty raises typed `cleanup_failed`.

Dwell uses injectable monotonic time with membership duration before Scene duration,
then default image/pair/web dwell or natural video EOF. Timed video ends at the
first of EOF and dwell expiry. Video state is inspected every 0.5 seconds; timed
image/web waits sleep until their deadline or a command. Global Pause freezes the
remaining budget and automatic progression, pauses actual video, and leaves image
or web content displayed (web scripts continue). Previous/Next use planner history
while retaining Pause; new video receives `start_paused=True`. Pause is transient
and is not a panel-safety override.

Navigation/state commands cancel the private in-flight operation token; cancellation
is not a failed-content attempt. Eight consecutive ordinary failed attempts retire
to degraded black/idle for five seconds before continuing the planner. A successful
presentation resets the budget. No active Sequence means intentional idle; disabled
selection also waits five seconds for reevaluation. Ineligible occurrences consume
the same bounded attempt budget. There is no configuration notification service.

`set_output_suppressed(bool)` clears content/audio and holds progression, retaining
the logical current identity and user Pause. Release revalidates/re-presents that
identity or enters normal bounded fallback. Video position restoration is not
promised. #9/#10 own suppression policy; this seam makes no physical-power claim.

Frozen thread-safe `status` contains fixed state/reason, composition IDs, remaining
dwell at the last update and optional bounded video progress/audio capabilities.
Media paths and web targets never enter it. No playback state is persisted.
Unexpected thread failure or uncertain cleanup sets a fixed `failure` and the shared
host stop event; no replacement content starts after authority loss. The owner must
call `join()` when that event wakes: join cancels/wakes the operation or dwell wait,
waits at most five seconds and raises on failed cleanup or an unresponsive worker.
The daemon flag permits fatal process exit, not clean abandonment. #93 must propagate
failure as nonzero runtime exit for systemd/cgroup cleanup; Flask remains independent.

## Scheduling and time semantics

Scheduling has two related but distinct responsibilities.

### Display operating schedule

#101 implements the durable configuration and pure current-instant evaluator in
`postcardscene.operating_schedule`. Migration `0010_operating_schedule` preserves
existing settings/domain/catalog data and defaults to schedule disabled, no
windows and no override. These fields and windows are durable appliance state.

V0 stores at most 64 weekly **active windows**, OR-composed, with Monday = 0
through Sunday = 6. Each row is a same-day half-open interval: start minute
0–1439, end minute 1–1440, start strictly before end. Full day is 0–1440;
overnight activity requires two explicit adjacent-day rows. Overlap is allowed.
Disabled means normally active; enabled with no matching windows means sleep.
Python validation and SQLite constraints enforce ranges; a SQLite insert trigger
also enforces the complete-set limit. Replacement validates all inputs before
changing enabled/windows together in one short write transaction.

`OperatingSchedule` and `WeeklyWindow` are frozen detached records.
`get_operating_schedule` (or caller-owned `read_operating_schedule`) reads one
bounded snapshot, including the existing `ApplicationSettings.timezone` IANA key.
Malformed persisted state raises `DatabaseError`, never an invented active/sleep
fallback. The evaluator accepts an aware UTC instant and returns active intent,
a fixed reason (`schedule_disabled`, `schedule`, `override`) and expired-override
cleanup eligibility. It reads no clock or database and performs no side effects.

Evaluation converts the supplied absolute instant to the application timezone.
Spring-forward skipped wall minutes never occur; repeated fall-back minutes obey
the same window in both occurrences. Seconds do not change minute membership.
Fresh snapshots consume timezone changes without rewriting windows. Clock jumps
and downtime converge directly to the current rule, with no future transition
jobs, missed-event replay or persisted runtime history.

One optional temporary bool override wins over either schedule baseline until
its absolute UTC Unix-second expiry. Its two settings fields are both null or
both populated. `set_temporary_override` accepts integer 1–10080 minutes (seven
days maximum) and an optional aware UTC instant; expiry uses whole Unix seconds.
`clear_temporary_override` resumes the schedule immediately. Expired overrides
are ignored and `clear_expired_override` compare-clears the observed state/expiry
pair so a different concurrent override survives. Once cleared, a backward clock
jump cannot resurrect it. Each mutation owns a short database-only transaction.

#102 adds independently constructible `runtime.schedule.ScheduleWorker`: one
stdlib thread sharing RuntimeHost cancellation, immediate evaluation and a fixed
15-second monotonic polling budget including operation time. Every pass reads a
fresh detached snapshot and the current aware UTC clock; missed transitions are
never queued or replayed. Waits are interruptible and shutdown joins within five
seconds. Injected synchronous targets must bound their work and honor cancellation;
an uncooperative call causes a fixed fatal shutdown timeout rather than a clean
shutdown claim. The daemon thread preserves process exit in that case.

The sole target takes active intent and a cancellation predicate, returning
`applied`, `unavailable`, or `cleanup_failed`. Successful unchanged state is not
reapplied. Read/evaluation and ordinary target failures retain the last successful
state and retry at the bounded cadence. Expired overrides use the baseline now
and compare-clear the exact observed authority; failed durable cleanup degrades
without reversing that decision. Target cleanup uncertainty is fatal, sets the
shared stop event, and prevents further transitions. Frozen lock-protected status
contains lifecycle, desired/applied booleans and fixed reasons only. No runtime
schedule status is persisted.

#103 exposes authenticated server-rendered schedule configuration using these
persistence helpers and request-time pure evaluation. Draft Add/Remove never
writes; complete Save replaces enablement/windows atomically. Overrides use
separate POST/CSRF actions. No request starts runtime work or sends commands.
The live playback/panel join (#104) remains unimplemented.
RuntimeHost does not construct this worker until its real operating target exists;
there is no temporary playback or panel owner. Keep-active intent never bypasses #10
panel/static-content protection; physical power capability remains separate.

### Content scheduling

Later content rules may control which sequence/scenes are appropriate at a given time. Rich conditional scenes are primarily V2 work.

Scheduling logic belongs to the long-running application runtime, not Flask request handlers.
