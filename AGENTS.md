# AGENTS.md

This file defines PostcardScene-specific rules for coding agents. General development workflow, Git safety, review discipline, and globally supplied agent capabilities come from the agent bootstrap and are intentionally not duplicated here.

## Repository authority

Treat the repository as authoritative. Before implementing or materially hardening an issue, read:

1. `README.md`
2. `docs/architecture.md`
3. `docs/roadmap.md`
4. this file
5. the owning epic and implementation issue

If implementation and documentation disagree, do not silently invent a new architecture. Resolve the inconsistency explicitly and update the relevant authority document in the same change when the decision is consequential.

## Deterministic issue execution protocol

The GitHub milestone/tracker/epic/child graph is the project execution authority. A fresh coding agent must not choose work merely because an issue looks interesting, has a low issue number, or is easy to implement.

For V0, start from tracker issue `#1` and use this algorithm:

1. Read `#1`, the four repository authority documents above, and the current milestone state.
2. Walk the incomplete V0 epics in the order listed by `#1`.
3. Cross-issue `Depends on`, `Blocks`, explicit prerequisite text, and authoritative architecture dependencies override display/list order. An item is **dependency-ready** only when every required prerequisite is complete or the owning issue explicitly permits parallel work.
4. Select the first incomplete dependency-ready epic in tracker order.
5. If that epic already has implementation children, follow its explicit `Suggested execution` order when present. Otherwise use the child checklist order. Select the first incomplete dependency-ready child.
6. If the selected epic has not yet been decomposed, harden/decompose the epic first: reread repository authority, resolve only decisions needed to make the work executable, create bounded child issues with parent/dependency links and acceptance criteria, and update the epic checklist/execution order. Do not treat an undecomposed epic as permission to implement the entire epic in one oversized change.
7. Implement one bounded child issue at a time unless the owning issue explicitly authorizes a cohesive combined slice. Run the required checks and update affected documentation in the same change.
8. Close a child only when its acceptance criteria and required evidence are satisfied. Keep the parent epic checklist accurate; do not rely on issue closure automatically updating every Markdown task list.
9. Close an epic only after every owned child is complete and the epic-level completion contract has been reread against the resulting repository.
10. Return to `#1` after each child/epic completion and resolve the next dependency-ready item using the same algorithm.
11. Close `#1` only after all V0 epics and the explicit V0 closure gates are satisfied for the exact release candidate.

Canonical precedence:

```text
explicit dependency / prerequisite
        >
tracker epic order
        >
epic Suggested execution
        >
epic child checklist order
        >
issue number
```

Issue number by itself is never scheduling authority. A cross-epic dependency can make a later-listed epic or child runnable before an earlier-listed epic is complete; dependency readiness wins.

### Parallel and blocked work

Parallel work is allowed only when the graph makes it independent. If a branch is blocked by unavailable physical hardware, an external prerequisite, or another genuine evidence dependency:

- report the blocker truthfully;
- do not mark the blocked issue complete;
- do not manufacture mock evidence for a physical/external requirement;
- continue only with a dependency-ready independent item that does not violate an explicit prerequisite or architecture boundary;
- return to the blocked gate when the required evidence becomes available.

For example, software decision logic around display hardware may proceed in CI, but the physical 4K/HDMI claims in `#31` remain open until representative hardware evidence exists.

### Milestone progression

V1 and V2 intentionally exist without speculative implementation backlogs.

- Finish the current V0 capability boundary and closure gates first unless the user explicitly reprioritizes the roadmap.
- When V1 or V2 becomes active, use its roadmap section to create the milestone tracker/epics just in time, then apply the same tracker -> epic -> child protocol.
- Do not pre-create large future backlogs solely to make a milestone look populated.

## Product boundary

PostcardScene is a self-hosted ambient media and information **display appliance**, not merely a slideshow and not primarily a web application.

The composition model is:

```text
Source -> Widget -> Scene -> Sequence
```

Preserve this model unless a separately reviewed architectural change demonstrates a better boundary.

The web interface is the control plane. The long-running runtime/player is the display/application runtime. Keep those concerns separated.

## Architecture rules

- Use Python as the application language.
- Flask is the initial web/control framework.
- Use SQLAlchemy with SQLite initially.
- Use Alembic/Flask-Migrate for schema migrations.
- Use Flask-Login for authentication and Flask-WTF or equivalent framework support for forms/CSRF where needed.
- Keep authorization simple initially; do not build enterprise-style permission machinery without a concrete requirement.
- Keep core source, catalog, player, scene, layout, scheduling, media, and hardware decision logic in ordinary Python modules that do not depend unnecessarily on Flask.
- Keep the web/control process and runtime/player process independently restartable.
- Chromium is the preferred rich scene renderer; mpv is the preferred video/audio playback engine.
- Use Linux/systemd integration for long-running services and display/media system integration.
- Do not introduce Docker as a requirement unless a later concrete deployment reason justifies it.
- Do not introduce Redis, Celery, RabbitMQ, a distributed queue, or a third always-on worker service without demonstrated need and an architecture update.
- Prefer a small number of mature dependencies over many thin framework extensions.

## Long-running work ownership

Do not put scans, reconciliation, schedules, renderer supervision, or future provider refresh loops inside Flask request handlers.

V0's default owner for long-running application work is the runtime/player process or a narrowly factored runtime component it hosts. This includes operating schedule evaluation, filesystem media reconciliation, renderer/player supervision, and later provider cache refresh.

Long-running work must have bounded/cancellable shutdown behavior. The runtime remains alive while the physical display is asleep so required schedules/reconciliation can continue.

## Composition model vs media catalog

The filesystem media catalog is a source/runtime data layer, not a fifth universal composition concept:

```text
Filesystem Source -> MediaItem catalog -> Widget selection
```

beneath:

```text
Source -> Widget -> Scene -> Sequence
```

Rules:

- `MediaItem` is normalized source/runtime data.
- Original photo/video bytes remain in their source filesystem.
- Do not copy media into SQLite merely to index it.
- Playback should consume the common catalog/source boundary rather than repeatedly walking entire filesystem sources.
- A temporary NAS outage is not authoritative deletion of cataloged items.
- Catalog scans/reconciliation must be bounded and cancellable.
- Do not add face recognition, computer vision, thumbnails, or broad EXIF indexing to V0 unless a concrete issue explicitly promotes that scope.
- Do not force future Immich/provider assets into the filesystem catalog design when their provider model does not need it.

## SQLite and migrations

SQLite is shared application state, not a distributed coordination service.

- Keep write transactions short.
- Use the journaling/timeout/connection policy frozen by the persistence issue.
- Avoid designs requiring multiple processes to hold long concurrent write transactions.
- Apply schema changes through Alembic/Flask-Migrate.
- Application startup must not silently rewrite an unexpected production schema.
- Preserve application/schema identity needed by release and backup/restore workflows.
- Background/catalog concurrency tests should exercise representative locking behavior at a stable seam.

## Media behavior

### Images

- Respect presentation/EXIF orientation.
- Preserve useful source quality for 1080p/validated-4K output; do not rewrite originals for normal playback.
- Make fit/fill/crop behavior deterministic.
- Keep portrait-pair selection in the normal media/runtime path rather than special filesystem code.
- Bound corrupt, unreachable, or extremely large image loads/decodes so one item cannot exhaust memory or stall the sequence indefinitely.
- Treat color/profile behavior deliberately and document platform limitations rather than making unsupported color-accuracy claims.

### Video and audio

- Use mpv through a supervised, validated subprocess boundary.
- Do not promise every codec/container; base support on the validated platform/runtime.
- Do not introduce automatic transcoding as an implicit V0 requirement.
- Bound media load/start failure.
- Audio state is explicit: enabled/muted/volume and intended output where supported.
- No video/audio may outlive the scene that owns it.
- Display sleep/off must leave media audio silent.

## Scene and sequence runtime

- Execute all V0 content through the shared scene/sequence runtime rather than parallel media-specific slideshow loops.
- Keep transient current/recent-play/previous-next state in the runtime unless a concrete restart contract requires a small durable field.
- Use stable scene/media identities for shuffle/history.
- Failed or unavailable scenes/items must use bounded skip/fallback behavior.
- Never enter a tight retry loop on unavailable content.
- If no eligible scene remains, converge to a defined safe blank/idle state and cooperate with panel protection rather than leave stale content indefinitely.

## Graphics session and display boundary

PostcardScene owns a Linux graphical display session; this is separate from physical panel power.

- Follow the graphics/session model selected by #31.
- Do not add a general desktop environment or interactive display manager unless the issue's evidence requires it.
- Chromium and mpv must target the same intended display/session predictably.
- Preserve safe blank/background behavior while renderers are unavailable.
- Handle boot without a display and later hotplug/reconnect according to the supported runtime contract.
- Hardware-specific 1080p/4K, GPU/decode/audio/CEC claims require representative physical evidence; CI cannot prove HDMI hardware behavior.
- Use normal device/group permissions where practical rather than broad root execution.

## Display power and panel protection

Display power management is a first-class subsystem and is **not** equivalent to stopping the renderer.

Keep physical display control behind a narrow abstraction with methods equivalent to:

```text
power_on()
power_off()
get_power_state()
```

Expected backend families are HDMI-CEC, DDC/CI, and DRM/KMS or HDMI signal control fallback.

- A failed power command must surface degraded/unknown state rather than claim success without evidence.
- Scheduled display sleep must also stop/silence media playback.
- Burn-in/static-content protection and renderer-stall handling belong to this subsystem/runtime cooperation, not incidental UI behavior.
- Do not grant the Flask process broad root privileges for display control.

## Scheduling and time

Scheduling must be timezone-aware and deterministic across real clock transitions.

- Keep one explicit application timezone.
- Define DST skipped/repeated-time behavior.
- Treat temporary/manual overrides as explicit state with persistence/expiry semantics.
- After reboot, long downtime, NTP correction, manual clock jump, or timezone change, converge to the state that should be active now rather than replay every missed transition.
- Cover midnight, week boundaries, DST forward/back, and large forward/backward clock changes.
- Scheduling belongs to the long-running runtime, not Flask requests.

## Network storage

Prefer normal Linux SMB/NFS mounts and expose mounted paths to PostcardScene as filesystem sources.

- V0 does not own NAS credentials or mounting policy.
- Never allow arbitrary mount commands or shell execution from user-provided web input.
- Path and symlink handling must remain within configured source authority.
- An unavailable/hung network source must not indefinitely block the control plane or unrelated playback.
- Never silently fall back to a similarly named local path when a configured remote mount is unavailable.

## Web scenes are untrusted content

Configured web pages are a separate trust boundary.

- Normal V0 web scenes should accept only supported HTTP/HTTPS URLs.
- Reject dangerous local/script/browser-extension schemes unless an explicit future trusted feature owns them.
- Use a dedicated Chromium kiosk profile/session separate from PostcardScene administration cookies/secrets.
- Do not inject generic usernames/passwords/tokens into URLs as an authentication feature.
- Persistent third-party kiosk sessions, if implemented, must use an explicit isolated mechanism.
- Disable unnecessary production remote-debugging/automation listeners or bind required control surfaces narrowly to local authority.
- Bound navigation/load retries/timeouts.
- Do not turn PostcardScene into a generic web-scraping/browser-automation platform.

## Authentication, network exposure, and secrets

Follow the trust boundary owned by the security epic.

- Never store plaintext passwords.
- Use a persistent protected installation-owned session/application secret; do not regenerate it on every boot or commit it to the repository.
- Protect state-changing web actions against CSRF.
- Keep production debug mode disabled.
- Do not expose the Flask development server as the normal production appliance boundary.
- Define production bind/HTTP/HTTPS/reverse-proxy behavior before V0 release rather than assuming a trusted LAN makes credential transport irrelevant.
- Provide a deliberate local/host-authorized administrator password-recovery path.
- Define credential-at-rest protection before storing long-lived third-party credentials.
- Keep secrets out of logs, errors, status/doctor output, command lines, release artifacts, tests, and backup manifests where the secret value is not explicitly owned by the archive.
- No external telemetry or analytics by default.

## Privilege and subprocess rules

- Keep the Flask process unprivileged.
- Prefer normal device/group permissions for DRM/render/video/audio/CEC access.
- If privileged helpers become necessary, expose only narrow allowlisted operations with strict argument validation.
- Prefer subprocess argument vectors; avoid `shell=True` and string-built commands from untrusted/user configuration.
- Validate URLs, paths, archive members, versions, and subprocess arguments at their owning boundary.
- Never recursively change ownership/delete outside exact known roots merely to make an installation work.

## Release, installation, and update

- Use one authoritative application version identity.
- Use deterministic dependency resolution/locking for release builds/production installs.
- Release artifacts must correspond to immutable source/tag identity and have checksums of the final published bytes.
- Rebuilding/repackaging different bytes creates a new artifact-specific candidate.
- Normal production installation should use an isolated project-owned Python environment or another approved mechanism, not unsafe privileged modification of distro-owned Python.
- Separate application payload, configuration/secrets, durable state, replaceable caches/runtime state, logs, and backups.
- Apply production schema changes explicitly through migrations.
- Forward updates that mutate schema/durable state require the recovery boundary owned by backup/restore.
- Do not interpret installing older application files as database rollback.
- Safe remove/uninstall preserves durable state and backups by default; destructive purge/factory reset is a separate deliberate action if ever implemented.
- Do not introduce an auto-update daemon in V0.

## Backup and restore

PostcardScene backup owns PostcardScene durable state, not the user's original media libraries.

- Use a SQLite-consistent backup method; do not blindly copy a live database file.
- Classify every application-owned path as durable, replaceable/regenerable, secret authority, or external/unowned.
- Decide explicitly whether the filesystem media catalog is backed up or regenerated.
- Use manifests with application/schema/archive identity and checksums.
- Publish backups atomically and delete only archives owned by the documented retention convention.
- Missing mounted remote backup storage must fail visibly; never fall back silently to an unintended local destination.
- Treat backup archives as sensitive.
- Test restoration, not only archive creation.
- V0 requires an in-place restore and a clean replacement-host restore path.
- Protected credentials must either restore with the matching key authority or be explicitly classified as requiring re-entry.
- An old backup is not automatically a safe application rollback; fail closed on unproven app/schema compatibility.

## UI and configuration

Important user-facing behavior should be configurable through the authenticated web settings UI where practical.

Do not expose raw internal database structures as the normal product UI merely because they are easy to generate.

Control UI changes should preserve the project's responsive/accessibility baseline:

- visible labels;
- keyboard operation;
- focus/error feedback;
- sufficient contrast through the chosen CSS system;
- usable desktop/mobile containment.

Do not create a large frontend framework or browser-test matrix merely for these basics.

## Reliability and observability

The display is intended to run unattended for long periods. Design for process crashes/restarts, host reboot/power loss, display hotplug, temporary NAS outages/hangs, malformed media, Chromium/mpv failure, renderer stalls, background/catalog interruption, future provider outages, and low disk space/cache/log growth.

Observability is shared by owning layers:

- web dashboard/status for user-visible health;
- runtime/systemd/journald for process/resource/runtime evidence;
- release/install `doctor`-style diagnostics for installation/configuration consistency.

Do not create a metrics/telemetry platform in V0 merely because diagnostics are required.

## Documentation requirements

When a change alters user-visible behavior, configuration, installation, security, recovery, hardware support, or a consequential architectural boundary, update the smallest relevant authority document.

Keep documentation lean. Prefer the current authority documents and a future cohesive `docs/operations.md` over many one-topic files until size/audience justifies splitting.

Before a public distributable release, include the project license and required third-party notices/attributions. Hardware/support claims must distinguish physically tested evidence from intended but unverified configurations.

## Project validation targets

In addition to the globally supplied testing/review workflow, PostcardScene changes should cover the relevant product-specific seams:

- source/path safety and symlinks;
- source/catalog reconciliation and outage recovery;
- media selection and portrait pairing;
- image orientation/pathological media bounds;
- video failure/audio lifecycle;
- scheduling/timezone/DST transitions;
- scene/sequence fallback behavior;
- SQLite migration/concurrency assumptions;
- authentication/session/CSRF;
- web-scene URL/profile isolation;
- display-power state decisions;
- backup integrity/retention/restore compatibility;
- security-sensitive validation/redaction.

Hardware-dependent decision logic should be testable without physical equipment, but actual HDMI/GPU/CEC/DDC/4K support claims require representative physical evidence rather than mocks alone.

## Project-local development tooling

Issue #12 establishes the initial project-local toolchain. Its target commands are:

```bash
uv sync --locked
uv run pytest
uv run ruff format .
uv run ruff format --check .
uv run ruff check .
uv build
uv run python -c "import postcardscene; import postcardscene.web; import postcardscene.runtime"
```

For #12 itself these commands define the bootstrap contract to make valid. After #12 lands, they are the canonical project-local commands unless a later issue deliberately changes the toolchain.

Do not add repository-specific copies, wrappers, or configuration for capabilities already supplied by the global agent bootstrap unless PostcardScene develops a concrete project-specific need.

## Scope constraints

Do not build speculative plugin systems, multi-tenant authorization, distributed workers, generalized event buses, configuration-management systems, browser automation frameworks, media-analysis pipelines, auto-update systems, or deployment/package-manager machinery solely because they may be useful someday.

Preserve these foundational PostcardScene boundaries:

- `Source -> Widget -> Scene -> Sequence` composition;
- source-owned filesystem catalog beneath `Source`;
- separate control and runtime processes;
- long-running work outside Flask requests;
- separate graphics-session and physical-panel-power ownership;
- explicit security/privilege boundaries;
- explicit durable/replaceable/backup state ownership.

## Open decisions

Some implementation choices are intentionally not frozen. The authoritative list is in `docs/architecture.md`.

Do not resolve an open decision globally before its owning issue requires the decision and has enough evidence. When a consequential decision is made, update `docs/architecture.md` in the same change.
