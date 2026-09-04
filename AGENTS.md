# AGENTS.md

This file defines the working rules for coding agents contributing to PostcardScene.

## Repository authority

Treat the repository as authoritative. Before making architectural changes, read:

1. `README.md`
2. `docs/architecture.md`
3. `docs/roadmap.md`
4. this file

If implementation and documentation disagree, do not silently invent a new architecture. Resolve the inconsistency explicitly and update the relevant documentation with the code change.

## Product boundary

PostcardScene is a self-hosted ambient media and information display, not merely a slideshow and not primarily a web application.

The core model is:

```text
Source -> Widget -> Scene -> Sequence
```

Preserve this model unless there is a strong implementation reason to change it.

The web interface is the control surface. The display/player is the product runtime. Keep those concerns separated.

## Architecture rules

- Use Python as the application language.
- Flask is the initial web/control framework.
- Use SQLAlchemy with SQLite initially.
- Use Alembic/Flask-Migrate for schema migrations.
- Use Flask-Login for authentication and Flask-WTF or equivalent framework support for forms/CSRF where needed.
- Keep authorization simple initially; do not build enterprise-style permission machinery without a concrete requirement.
- Keep the core player, source, scene, layout, scheduling, and media logic in ordinary Python modules that do not depend unnecessarily on Flask.
- Keep the web/control process and player/display process independently restartable.
- Chromium is the preferred rich scene renderer; mpv is the preferred video playback engine.
- Use Linux/systemd integration for long-running services and display/media system integration.
- Do not introduce Docker as a requirement unless the project later gains a concrete deployment reason for it.
- Do not introduce Redis, Celery, RabbitMQ, or similar infrastructure without a demonstrated need.
- Prefer a small number of mature dependencies over many thin framework extensions.

## Data and integrations

External data providers should be represented behind source/provider abstractions rather than leaking provider-specific logic throughout scenes.

Expected source families include:

- local directories
- SMB/NFS-mounted directories
- Immich
- Wayfarer
- arbitrary URLs
- weather providers
- RSS/Atom feeds
- generic HTTP/JSON sources

Use `requests` or the project's shared HTTP abstraction for synchronous HTTP access unless an asynchronous requirement is demonstrated. Use explicit timeouts. Retries must be bounded and appropriate for the operation.

External data should normally be cached or collected independently of rendering. A scene should not become unusable simply because a remote request is slow or temporarily unavailable.

## Context-aware scenes

Provider data may contribute to shared scene context. For example, a Wayfarer live location may drive:

- the live map position
- current local weather
- local time/place information
- geographically relevant Immich image selection

Do not hard-code these combinations into one monolithic integration. Prefer reusable sources/widgets consuming shared context.

## Display and power

Display power management is a first-class subsystem.

Keep physical display control behind a narrow abstraction with methods equivalent to:

```text
power_on()
power_off()
get_power_state()
```

Expected backends are:

1. HDMI-CEC
2. DDC/CI
3. DRM/KMS or HDMI signal control fallback

Do not grant the Flask process broad root privileges to achieve display or mount control. If privileged operations become necessary, use a narrowly scoped helper or controlled system service.

Burn-in/static-content protection and renderer-stall handling are part of the display subsystem, not incidental UI behavior.

## Network storage

Prefer normal Linux SMB/NFS mounts and expose mounted paths to PostcardScene as media sources.

If the web UI later manages mounts, use a narrowly scoped privileged mechanism. Never allow arbitrary mount commands or shell execution from user-provided web input.

## UI and configuration

Important user-facing behavior should be configurable through the web settings UI where practical, including:

- media sources
- scenes and sequences
- schedules
- display power method and fallbacks
- wake delays
- static-content/burn-in protection
- source refresh intervals
- playback timings and transitions

Defaults should make the system useful without requiring constant administration.

Do not expose raw internal database structures as the normal product UI merely because they are easy to generate.

## Scope discipline

Follow `docs/roadmap.md` and implement the smallest coherent milestone first.

Do not build speculative plugin systems, multi-tenant authorization, distributed workers, elaborate event buses, or generalized abstractions solely because they may be useful someday.

However, do preserve the foundational `Source -> Widget -> Scene -> Sequence` separation from the beginning so the MVP does not have to be rewritten to support richer scenes later.

## Reliability

The display is intended to run unattended for long periods.

Design for:

- process crashes and restarts
- temporary NAS unavailability
- temporary internet/API failures
- malformed media
- Chromium/mpv failure
- renderer stalls
- display disconnect/reconnect
- host reboot

Failures in one source or renderer should degrade gracefully rather than take down the control interface or the entire sequence engine.

## Security

- Never store plaintext passwords when password hashing is appropriate.
- Protect state-changing web actions against CSRF.
- Validate and constrain file paths, URLs, mount configuration, and subprocess arguments.
- Avoid `shell=True` and arbitrary shell construction.
- Keep external credentials/API keys out of logs.
- Do not expose unrestricted filesystem access through media-source configuration.
- Keep privileged operations outside the normal Flask process.

The initial deployment may be a trusted home network, but do not rely on that assumption to justify unsafe implementation patterns.

## Tests

Add tests with new behavior where practical. Prioritize tests for:

- media/source selection
- portrait-pairing logic
- scheduling and time-window behavior
- scene/sequence rules
- provider failure and cache fallback
- display power state decisions
- security-sensitive validation

Hardware-dependent code should be structured so decision logic can be tested without requiring a physical TV, monitor, NAS, or Raspberry Pi in CI.

## Open decisions

Some implementation choices are intentionally not frozen yet. They are listed in `docs/architecture.md`.

Do not resolve them globally before a concrete implementation milestone requires the decision. When a consequential decision is made, update the architecture documentation in the same change.
