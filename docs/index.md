---
layout: default
title: PostcardScene documentation
---

# PostcardScene documentation

PostcardScene is a self-hosted ambient media and information display appliance
for Linux. Its web interface configures Sources, Widgets, Scenes and Sequences;
a separate runtime owns the display and background work.

[View on GitHub](https://github.com/stef-k/PostcardScene)

V0 is under development. The guides distinguish implemented capabilities from
unfinished playback integration and unverified physical Raspberry Pi/HDMI support.
Check the relevant support and evidence limits before deploying an appliance.

## Start here

1. Review [hardware expectations and validation limits](display-hardware.md) and
   [managed Linux host prerequisites](operations/installation.md#read-only-managed-host-preflight-139).
2. Follow [release verification](operations/installation.md#verified-github-release-bundles-143)
   and [installation and first-run setup](operations/installation.md#managed-initial-installation-140).
3. Configure [Sources](sources.md), then [Widgets, Scenes and Sequences](composition.md).
   The composition guide explains which playback capabilities still await integration.

## Administration and everyday use

The [Appliance operations index](operations.md) leads to the installation,
runtime and display guides. Use these existing guides for detailed procedures:

| Task | Guide |
| --- | --- |
| Installation, deployment and host configuration | [Installation and diagnostics](operations/installation.md) |
| Sources, trusted media roots, NAS outages and catalog refresh | [Sources](sources.md) |
| Everyday usage, composition and playback selection | [Composition configuration](composition.md) |
| Services, logs, status and troubleshooting | [Runtime and services](operations/runtime.md) |
| Timezone, weekly schedules and temporary overrides | [Schedule configuration](operations/runtime.md#weekly-schedule-and-temporary-overrides) |
| Display configuration, graphics, video/audio and panel power | [Display, rendering and panel control](operations/display.md) |
| Backup creation and verification | [Manual sensitive backups](operations/installation.md#manual-sensitive-backups-158) |
| Automatic backup configuration and status | [Backup policy](operations/installation.md#backup-policy-and-advisory-status-165) |
| In-place and replacement-host recovery | [Managed same-version restore](operations/installation.md#managed-same-version-restore-170) |
| Upgrade and interrupted-update recovery | [Managed forward update](operations/installation.md#managed-forward-update-177) |
| Safe removal and reinstall | [Managed remove and reinstall](operations/installation.md#managed-remove-and-reinstall-142) |
| Security, network exposure, HTTPS and signing secrets | [Production control plane](operations/runtime.md#production-control-plane-131) |
| External web-content isolation | [Web URL Sources](sources.md#web-url-sources) |

## Technical and contributor reference

- [Architecture](architecture.md): system boundaries and subsystem contracts.
- [UI design](ui-design.md): control-interface design and accessibility conventions.
- [Roadmap](roadmap.md): V0 completion gates and later capability milestones.
- [Development and build notes](https://github.com/stef-k/PostcardScene/blob/main/README.md#development).
- [Source repository](https://github.com/stef-k/PostcardScene) and
  [releases](https://github.com/stef-k/PostcardScene/releases).

Documentation follows implemented behavior: feature changes update the smallest
relevant published guide in the same PR. Extend these guides before introducing
a dedicated page; add one when its content warrants it. The later V0 documentation
and release audit remains tracked by [#28](https://github.com/stef-k/PostcardScene/issues/28).
