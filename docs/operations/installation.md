# Installation and diagnostics

Use this guide for release inputs, host preflight, managed initial installation,
failure recovery and installed read-only diagnostics.

Return to [Appliance operations](../operations.md). For installed service operation,
see [Runtime and services](runtime.md); for graphics and panel operation, see
[Display, rendering and panel control](display.md).

## Release inputs (#138)

Released CPython 3.11–3.14 is the application compatibility boundary. Generic
Linux CI is Python evidence only; Raspberry Pi ARM64/graphics/device support
still requires the owning physical validation gates.

The versioned application input is `postcardscene-<version>-py3-none-any.whl` plus
`runtime-requirements.txt`, generated from the exact `uv.lock`. Installation must
use hashes and binary dependencies only, followed by the wheel with `--no-deps`;
a missing compatible dependency wheel fails rather than compiling on the target.
No configuration, signing key, database, cache/profile or backup belongs in these
release inputs.

The GitHub Release artifact set is one native archive, the same application wheel
for audit/development, and external `SHA256SUMS`. No optional SBOM is adopted in
this change; dependency/security assessment remains with #135.
See [development checks](../../README.md#release-input-development) and the
[release contract](../architecture/installation-recovery-and-operations.md#deterministic-release-inputs-138).

## Verified GitHub Release bundles (#143)

A candidate tag must be exactly `v<pyproject project.version>` (currently
`v0.1.0.dev0`). Push that already-reviewed tag, or deliberately run the Native
release workflow **on that tag**. There is no version override or tag creation.
The workflow validates a clean exact checkout with pinned uv 0.12.10, builds and
smokes final archive bytes, and publishes through a draft. GitHub prerelease
status follows the validated package/tag version: development and prerelease
versions (such as `0.1.0.dev0` or `0.1.0rc1`) become prereleases; final stable
versions (such as `0.1.0`) become normal releases. There is no classification override.
Tag, source commit, wheel, installed version and manifest must agree. An existing
release is never overwritten. Failed upload/publication is a failed candidate;
inspect any incomplete draft before recovery. Release creation is separate from
V0 readiness, security, recovery and physical validation closure.

Download from the intended repository release into a new private directory:

```bash
mkdir -m 700 postcardscene-download
cd postcardscene-download
gh release download v0.1.0.dev0 --repo stef-k/PostcardScene \
  --pattern postcardscene-0.1.0.dev0-linux-native.tar.gz \
  --pattern postcardscene-0.1.0.dev0-py3-none-any.whl --pattern SHA256SUMS
sha256sum --check --strict SHA256SUMS
```

**Stop if checksum verification fails. Verify before executing any extracted
installer or support code.** Obtain `SHA256SUMS` through the trusted GitHub Release
channel alongside the archive; the internal manifest cannot authenticate its own
containing archive. Checksums establish correspondence to that release authority,
not an independent publisher signature.

After successful verification, extract into a separate empty directory as an
unprivileged user, then invoke the installer in an interactive terminal:

```bash
mkdir -m 700 bundle
tar --extract --gzip --file postcardscene-0.1.0.dev0-linux-native.tar.gz \
  --directory bundle --no-same-owner --no-same-permissions
cd bundle
sudo python3 -B install.py install
# To remove a recognized managed installation while preserving durable authority:
sudo python3 -B install.py remove
```

Substitute the chosen release version consistently. Keep the extracted directory
private and unchanged, containing exactly the wheel, `runtime-requirements.txt`,
`install.py`, `install_inputs.py`, `install_host.py`, `install_preflight.py` and
`release-manifest.json`. Place downloads, checksum files and diagnostics outside
it. The same bundle supports clean install, remove and compatible preserved-state
reinstall through the lifecycle described below.

The fixed schema-1 manifest records version/tag/source SHA, package Python range,
managed target classes, packaged SQLite application/Alembic identity, and exact
size/SHA-256 for each of the other six members. It does not hash itself.
`install_inputs.py` checks this closed schema and extracted member set before
host/preflight helper execution or any install/remove/reinstall host mutation.
The input validator itself first passes the code-owned pin in `install.py`;
all existing helper/requirements pins remain mandatory. `install.py` is the
initial bootstrap authority authenticated by the external archive checksum;
it does not pin the manifest, avoiding circular trust.

Archive construction uses sorted regular flat members, mode 0644, zero UID/GID,
empty owner names, USTAR metadata and the source commit timestamp for tar/gzip
and `SOURCE_DATE_EPOCH` for wheel building. The build rejects unsafe or unexpected
archive members before extracting. Reproducibility is scoped to identical source,
pinned tools and compression/build environment; different upstream tool/platform
bytes are not assumed identical. Every rebuild/repack with different bytes is a
new candidate requiring fresh external checksums and smoke evidence. Never reuse
an earlier archive checksum or readiness result.

Quality and release smoke consume the just-built archive, verify external sums,
safely extract its exact fixed members, validate installer pins/manifest and the
`install`/`remove` surface, then install its binary hash-locked dependencies and
wheel in a fresh supported Python venv. Installed metadata, entry points and
assets are checked outside the checkout. This is generic Linux artifact evidence;
the existing privileged lane owns real two-UID remove/reinstall evidence. Neither
proves ARM64 provisioning, physical Pi/HDMI behavior or final security closure.
Manual backup is available through #158 below. Forward update (#144) and restore
(#160) remain unavailable.

## Read-only managed-host preflight (#139)

Run `python3 -B install_preflight.py` from the source/native support files, or add
`--json` for the frozen result schema (`ok`, fixed `reasons`, nullable `plan`).
This standalone stdlib module needs no installed PostcardScene or root execution.
#140 `install.py` consumes `preflight()` directly; #143 ships the module
beside it. Exit 0 means the clean host has a representable provisioning plan;
exit 1 means inspection failed and there is no plan. It does not mean packages
are already installed, conflicts resolved, or physical playback validated.

| Managed target (Raspberry Pi 4/5-class ARM64) | Package authority |
| --- | --- |
| Ubuntu Server 24.04 LTS / Noble | Ubuntu archive native tools; Canonical Chromium snap, `latest/stable`, `/snap/bin/chromium`. |
| Ubuntu Server 26.04 LTS / Resolute | Same authority and native runtime contracts. |
| Raspberry Pi OS 64-bit / Debian 13 Trixie, preferably Lite | Debian/Raspberry Pi archive native tools and `chromium`, `/usr/bin/chromium`. |

Point releases retain these release identities. Exact `ubuntu` with its matching
release/codename, or `debian`/`raspbian` 13 with `trixie`, are recognized; `ID_LIKE`
does not authorize derivatives. Machine ARM64, 64-bit bootstrap userland and dpkg
`arm64` must agree. Containers, WSL and hosts without systemd PID 1 are rejected.
This is the managed target matrix, not proof of Raspberry Pi model or HDMI support.

The fixed apt set includes `labwc`, `wlr-randr`, `wlopm`, `mpv`, `ddcutil`,
`v4l-utils` (owns `/usr/bin/cec-ctl`), `python3`, `python3-venv`, `systemd`,
`systemd-sysv`, `libpam-systemd` and `libseat1`, plus Ubuntu `snapd` or Debian
`chromium`. Other native tool paths are `/usr/bin/<tool>`. Default seat authority
is active logind plus PAM; `--seat seatd` explicitly adds `seatd` and a socket
access provisioning action, without automatic fallback. The packaged #62 PAM/VT
and seatd templates remain authority for later installation.

`/usr/bin/python3` must already provide distro-owned CPython 3.11–3.14 and
importable venv/ensurepip with an available pip version. Missing bootstrap support
fails `python_bootstrap_required`; an operator must supply the distro prerequisite
before retrying. Preflight never creates a venv or installs into system Python.
Missing native tools return `install_required` only when cached apt candidates
have supported origins. No apt update/download is performed. Candidate and
installed versions must be represented by the official Ubuntu ports/archive/
security or Debian deb/security/Raspberry Pi archive URLs; custom mirrors and
locally supplied package versions require explicit operator resolution.

Installed executable authority is checked against dpkg ownership/integrity and
root-controlled paths before bounded version probes. Modified packages, missing
installed executables and malformed versions fail closed. Chromium uses package
or local snap metadata, never a browser launch. Ubuntu's `chromium-browser` is
only a transition to the snap; its wrapper alone cannot satisfy Chromium.
Installed labwc retains #62's 0.7.1 floor; no new media/panel version minimum is
invented. Package presence cannot prove seat, codec, audio, CEC or DDC behavior.

The installed roots in the table below, plus `/opt/postcardscene`, are
inspected only through existing ancestors/targets. Symlinks, foreign/writable
ancestors and unrecognized existing roots fail closed. Supported local persistent
filesystems are ext2/3/4, btrfs, xfs, f2fs and zfs; `/run` may also use tmpfs.
Unknown/network mounts are rejected before inspecting their installation paths.
Existing PostcardScene accounts or units are unrecognized until #140/#142 define
managed recognition; even empty roots are never adopted. Loaded tty1 getty or
display-manager units produce explicit `reserve_tty1`/`resolve_display_manager`
actions, not implicit permission to stop or disable them.

This snapshot makes no package/user/config/service/database mutation, recursive
root scan or repair. Commands use fixed argv, a clean environment, five-second
limits and 256 KiB output caps; failures omit raw output, identities and paths.
The later installer must recheck current authority before mutation and explicitly
handle every action. It must not treat serialized JSON as trusted installer input.
Fixture tests and local Ubuntu x86/WSL rejection/package-query observations are
software evidence only. ARM64 boot, real seat/device access and physical display
validation remain with #66/#116/#127 and the release-candidate gates.

Package evidence checked for #139: [Ubuntu release families](https://packages.ubuntu.com/),
[Noble labwc 0.7.1/arm64](https://packages.ubuntu.com/noble/labwc),
[Resolute wlopm](https://packages.ubuntu.com/resolute/wlopm),
[Ubuntu Chromium transition](https://packages.ubuntu.com/en/chromium-browser),
[Canonical Chromium snap](https://snapcraft.io/chromium),
[Trixie Chromium](https://packages.debian.org/trixie/chromium) and
[labwc](https://packages.debian.org/trixie/labwc),
[v4l-utils arm64 file list](https://packages.debian.org/trixie/arm64/v4l-utils/filelist),
and [Raspberry Pi OS Trixie images](https://www.raspberrypi.com/software/operating-systems/).
These are package/provisioning evidence, not hard-coded current version promises.

## Managed initial installation (#140)

From one trusted, reviewed extracted input set, run `sudo python3 -B install.py
install` in an interactive terminal. Keep exactly one application wheel beside
`install.py`, `install_inputs.py`, `install_host.py`, `install_preflight.py` and
`runtime-requirements.txt` and `release-manifest.json`; no checkout or
preinstalled application is needed. The installer validates wheel identity,
Python/pure-wheel metadata, entry points, required assets and wheel RECORD hashes.
It checks all three support modules and requirements against SHA-256 pins owned
by `install.py` **before executing any support code or mutating the host**.
Support reads reject symlinks, multiple hard links, non-regular files and oversized
inputs; the loader executes only the verified bytes, without sibling imports.
`install.py` owns CLI/lifecycle ordering, `install_inputs.py` deterministic wheel/input
validation, `install_host.py` fixed provisioning primitives, and
`install_preflight.py` the existing read-only host gate. The installer itself is
trusted executable bootstrap authority. These checks do not authenticate a
published release alone: first verify the external archive checksum as described
above. The manifest then verifies the extracted member set before host mutation.

The only initial path uses #139's default logind/PAM plan. Standalone preflight's
explicit seatd inspection remains available; this initial command does not select
seatd. Failed preflight or a non-root/non-interactive invocation changes nothing.
Existing accounts, roots, config or units fail closed, including repeat invocations;
installation never adopts legacy deployments, resets an administrator or replaces
a signing key. Exact preserved-state reinstall is documented below; updates remain #144.

The mutation sequence is:

1. Install the closed apt prerequisites (600-second update/900-second install
   bounds) and, on Ubuntu, Canonical Chromium stable snap (600 seconds). Recheck
   the clean-host preflight and require every planned tool to be installed before
   creating application roots. Package failures retain distro package-manager
   evidence and never trigger package rollback or application-state mutation.
2. Create a root-controlled `/opt/postcardscene/releases/<wheel-version>/venv`,
   using the supported distro Python. Install pinned requirements with required
   hashes and binary-only policy, then the app wheel with `--no-deps`. Check pip
   consistency, installed version, imports, entry points and packaged files before
   bootstrap or activation. No distro-Python pip installation or source build runs.
3. Create locked, nologin `postcardscene` and `postcardscene-web` users with shared
   primary group `postcardscene`. Runtime supplementary groups are selected only
   from actual root-owned group-readable/writable DRM, sound, CEC and I2C character
   devices and their allowlisted `render`/`video`/`audio`/`i2c` groups. Logind owns
   session device access. Web receives no supplementary/device/seat groups.
4. Provision the authorities below, copy wheel-owned systemd/PAM assets, and keep
   labwc configuration in the versioned wheel where its launcher already reads it.
   Install web/runtime `UMask=0007` drop-ins and a tmpfiles entry for reproducible
   `/run/postcardscene` creation. Record prior getty/display-manager states and
   local unit symlink targets in root-controlled
   `/opt/postcardscene/service-conflicts.json`, disable/stop loaded conflicts and
   mask tty1 getty. No desktop profile is rewritten.
5. Require inactive application services. Exclusively reserve the new empty
   database as the runtime UID with mode `0660`: SQLite's default initial `0644`
   cannot acquire group write through umask alone. Alembic owns all DB content.
   As the web UID with umask `0007`, run
   packaged `auth init-secret`, `db upgrade`, `db check` and `auth create-admin`.
   Username/password prompts use the interactive CLI; passwords never enter argv,
   environment, config or an installer log. No default password is generated.
6. Atomically create `/opt/postcardscene/venv` pointing to the validated release
   venv. Only then daemon-reload, enable graphics/runtime/web for multi-user boot,
   start them in that order and check active state. Existing independent service
   lifetime policies remain intact. Active units do not prove display readiness.

| Authority | Installed owner/group and mode |
| --- | --- |
| `/opt/postcardscene` and releases | root-controlled, directories 0755; payload not service-writable |
| `/etc/postcardscene`, `config.py` | root:postcardscene 0750 / 0640 |
| `/var/lib/postcardscene` | postcardscene:postcardscene 2770; SQLite/WAL/SHM 0660 |
| `/var/lib/postcardscene-web`, `session.key` | postcardscene-web:postcardscene-web 0700; web-owned key 0600 |
| `/var/cache/postcardscene` | postcardscene:postcardscene 0700 |
| `/run/postcardscene` | postcardscene:postcardscene 0750; panel socket 0660 |
| `/run/postcardscene-wayland` | graphics unit-owned 0700, independent of shared IPC |

Initial config selects the local shared database, private signing-key path and
loopback trusted Hosts. #131's direct loopback HTTP defaults remain in effect;
remote HTTP is not enabled. No provider credentials are written. Trusted roots
must be real directories; existing files are never overwritten and unknown trees
are never recursively chowned/chmodded.

### Install failures and evidence

Failure exits nonzero and reports the failed phase. Before bootstrap begins,
cleanup removes only the newly staged release; created config/identities/roots,
package changes and conflict records remain for inspection. After bootstrap starts,
payload, config, database and key are preserved without downgrade or rollback.
Activation failures attempt to disable and stop the new application units; a
failed stop/disable is reported separately and requires operator attention before
recovery or reboot. Do not delete durable authority or retry this as an update.

For a bootstrap failure, keep services inactive and inspect the named release's
venv and `/etc/postcardscene/config.py`. A host administrator can open a shell as
`postcardscene-web`, set `umask 0007` and
`POSTCARDSCENE_CONFIG=/etc/postcardscene/config.py`, then use that staged venv's
`python -m flask --app postcardscene.web:create_app` with `auth init-secret`,
`db upgrade`, `db check` and, only if initial creation did not complete,
`auth create-admin`. These existing commands validate/reuse key/schema authority;
first verify the database is runtime-owned, group `postcardscene`, mode `0660`.
If reservation itself failed, investigate the conflict before any migration;
do not let recovery implicitly create a replacement database with SQLite defaults.
never use password reset as an install retry. Have the host administrator verify
all phases before creating the active symlink or enabling services. Earlier
partial provisioning requires deliberate host reconciliation; this command has
no automatic cleanup/adoption/resume engine. The saved conflict record identifies
which prior boot services were changed for later safe removal.

The privileged disposable Linux CI smoke stages the real wheel at the installed
paths, creates genuinely distinct UIDs, uses normal persistence transactions in
both directions with live WAL/SHM sidecars, and exercises the group-authorized
panel socket. It proves private-key, socket replacement, Wayland and representative
character-device DAC separation and starts canonical runtime/web units under their
installed identities. It deliberately bypasses ARM64/package detection on the CI
VM. Graphics boot, actual distro provisioning on ARM64, HDMI/GPU/audio and physical
Pi behavior remain unverified; #66/#116/#127 retain that evidence.

## Installed read-only diagnostics (#141)

Run `/opt/postcardscene/venv/bin/postcardscene-doctor` as an authorized local host
administrator (normally via `sudo`); add `--json` for the same ordered, immutable
check results in JSON. No configuration-path, command, impersonation or repair
options are accepted. Exit **0** means all applicable checks are ready; **1** means
degraded/unavailable checks need attention; **2** means an installation identity or
configuration inconsistency (or invalid invocation); **3** is an internal command
error. `backup: not_applicable (not_implemented)` remains expected until #159 adds
persisted backup status; doctor does not probe destinations.

Fixed check identifiers cover release/wheel identity, exact #140 assets and
conflict-record structure, config/two-UID/key/DB/sidecar/IPC permissions, independent
service states, DB/schema/catalog health, #123 storage thresholds, web serving,
graphics and executable panel-tool prerequisites. No secrets, hostnames, media
paths, URLs, symlink targets, raw configuration or tool/error output are printed.
Each check has a five-second deadline and isolated failure; systemd output is
limited to 16 KiB and three allowlisted properties. Storage warning/critical
thresholds remain exactly #123's policy.

Doctor never opens the installed database through SQLite, which could create
sidecars even for a read-only query. It checks a private, disposable copy of only
the main DB and existing WAL (64 MiB combined maximum), rejecting files that
change while copied, then reuses `Database.check()` and persisted catalog health.
Private 0700 scratch under `/tmp` and 0600 copies are removed after each check,
including a check timeout. This is diagnostic sampling, **not a backup**. A busy,
oversized or unreadable DB reports unavailable; catalog summaries cap Sources at
1,000. Installed DB/WAL/SHM and signing-key authority are never repaired or created.

To avoid executing host code, configuration inspection accepts literal Python
assignments only. Valid dynamic Python remains service-owned and reports
`config_not_inspectable`; doctor does not execute it. Serving classification uses
the existing #131 validator without a listener. Graphics checks use the existing
session and read-only output probes only under the runtime UID; outside that UID,
including root, `capability_unavailable` is expected. Tool readiness means executable
DAC prerequisites only, not device access or physical panel support.

Doctor does not restart services, migrate, rescan media, read signing-key bytes,
parse journals, mutate hardware or establish HDMI/4K/acceleration evidence. Use
#140/#142 for provisioning/reinstall ownership, #144 for recovery-backed updates,
#27 for backup/restore, and #66/#116/#127 for physical validation. Review individual
service results independently; web failure does not imply runtime failure.

## Managed remove and reinstall (#142)

Use the trusted extracted input set matching the installed wheel and run
`sudo python3 -B install.py remove`. The complete helper/requirements set is
SHA-verified before any helper executes. Removal requires exact managed root,
identity, asset, configuration, database and key authority; source checkouts,
foreign layouts and partial installations are never adopted. Dynamic Python
configuration that cannot be inspected without execution requires manual
reconciliation, as with doctor.

The four lifecycle states are `clean`, `installed_managed`, `removed_preserved`
and `partial_or_unknown`. Only the first accepts initial installation. Only
`installed_managed` accepts removal. A normal successful removal leaves exactly:

```text
/opt/postcardscene/                         root:root 0755
/opt/postcardscene/service-conflicts.json   root:root 0644
/etc/postcardscene/config.py                existing config authority
/var/lib/postcardscene/                     existing DB and durable state
/var/lib/postcardscene-web/                 existing private signing authority
postcardscene + postcardscene-web           existing users and groups
```

Config directory authority, DB/admin credentials, signing key and backups remain
unchanged. The active venv, releases directory, canonical units/drop-ins/PAM/
tmpfiles assets, cache and both runtime directories are absent. Host prerequisite
packages remain installed; no uninstall/autoremove, purge or factory reset exists.
The retained root-controlled conflict record is required ownership evidence.
Do not delete, edit or replace it with a symlink to make a partial state appear
managed.

Removal stops/disables only the three PostcardScene units, verifies that no
service-UID process survives (with a bounded 15-second retirement grace), then restores recorded getty/display-manager state
before deleting validated payload/assets and replaceable roots. Unexpected
owners, symlinks, mounts, socket residue, changed assets or uncertain service
restoration cause nonzero failure and preserve uncertain targets. There is no
recursive ownership repair. Repeating remove on exact `removed_preserved` succeeds
without deleting anything or replaying boot-service restoration.

For reinstall, run `sudo python3 -B install.py install` from a compatible trusted
input set. The installer must first positively recognize `removed_preserved`.
Only then can it request the internal preserved-state prerequisite mode. The
standalone preflight CLI continues to require a clean host and has no adoption
switch. Distro, architecture, Python, package/tool, seat and local-filesystem
policy are unchanged.

Reinstall stages a fresh payload and validates a private DB snapshot against the
incoming application's schema/identity and existing administrator. It validates
the existing key as the web UID. It never runs migrations, admin bootstrap or key
initialization. It then recreates replaceable authority, atomically refreshes the
conflict record from current host state, reserves graphics and activates services.
Operator getty/display-manager changes made while removed become the state
restored by the next removal. Installing older bytes is not database rollback;
unproven compatibility requires the future update/restore workflow.

On any failure, preserve config, DB, key, marker and remaining assets. Inspect
protected service/package diagnostics and reconcile the reported phase manually;
a partial state cannot be repaired by simply rerunning installation. Staged
payload cleanup is limited to an exact validated inactive release. Before durable
reprovisioning, reinstall also removes its newly created, empty root-owned
`releases` parent, restoring exact `removed_preserved` state. Uncertain cleanup
authority is retained and reported for inspection. Reinstall success requires coherent installed authority and active services. Run
`postcardscene-doctor` afterwards for current installation diagnostics.

The privileged Linux smoke exercises remove/reinstall with real distinct UIDs,
SQLite/admin/config/key preservation, fresh conflict capture and doctor checks.
Its x86 package/platform and graphics-start substitutions provide software/DAC
evidence only, with no physical Raspberry Pi, seat, HDMI or 4K claim.


## Manual sensitive backups (#158)

Run the one backup CLI as the managed web identity, which can already read the
shared DB, managed configuration and private signing key:

```bash
sudo -u postcardscene-web /opt/postcardscene/venv/bin/postcardscene-backup create --destination /absolute/backup-directory
sudo -u postcardscene-web /opt/postcardscene/venv/bin/postcardscene-backup list --destination /absolute/backup-directory
sudo -u postcardscene-web /opt/postcardscene/venv/bin/postcardscene-backup verify /absolute/backup-directory/postcardscene-backup-20260910T120000000000Z-v0.1.0.dev0.tar.gz
```

Use the actual filename returned by `create`/`list` for verification. Commands emit
sanitized JSON and return zero on success, one on operation failure, or two for
CLI usage errors. List reports `verified`, `incomplete`, or `invalid` for recognized
backup names. An invalid/incomplete row is not a recovery point, even if listing
itself exits zero. Verification does not restore or change installed state.

The destination must already exist, be accessible to `postcardscene-web`, and be an
absolute local or already-mounted directory with no symlink components. Provision
its permissions and any mount/transport outside PostcardScene. Ensure an intended
remote destination is actually mounted before invoking the CLI; a path alone is
not evidence of a particular remote mount. There is no mount command, credential
management, cloud provider or fallback path. The destination filesystem must
support fsync and Linux atomic no-replace renames; unsupported operations fail.

Each `postcardscene-backup-YYYYMMDDTHHMMSSffffffZ-v<version>.tar.gz` contains exactly:

| Member | Recovery role |
| --- | --- |
| `postcardscene.sqlite3` | Online SQLite-consistent snapshot, including account and application state. |
| `config.py` | Exact managed `/etc/postcardscene/config.py` bytes. |
| `session.key` | Exact existing `/var/lib/postcardscene-web/session.key` bytes. |
| `backup-manifest.json` | Fixed v1 identity and DB/config/key sizes and SHA-256 hashes. |

The database may contain media catalog rows, classified in the manifest as
`regenerable_reconcile_required`. Restore-time invalidation/reconciliation belongs
to #160. Original media, provider assets, browser profiles, caches/runtime files,
logs, releases/venv, systemd/PAM assets and `service-conflicts.json` are excluded.
Do not use this archive as a host image or a backup of external media libraries.

**Archives contain sensitive account data, password hashes, private paths/config
and a signing key. PostcardScene does not encrypt them.** Protect destination and
transport confidentiality yourself, including any filesystem snapshots/copies.
Created archive/sidecar/temp files are 0600 and scratch directories 0700. Checksums
detect corruption; someone able to replace both archive and sidecar can forge a
pair. Never dump archive/config/key contents into logs or support messages.

Build happens in private host-local `/tmp` scratch, ignoring `TMPDIR`. Allow local
space for capture, the compressed archive, and a second archive/snapshot during
post-publication verification (up to about 18 GiB at the fixed maximum). Limits
are 4 GiB for the DB, 64 KiB for config, exactly 32 bytes for the key, 8 KiB for the
manifest and 5 GiB compressed archive. DB capture has a 120-second deadline;
each create/verify/list call allows 300 seconds overall and 30 seconds without
progress, plus bounded worker cleanup. Large or slow backups can therefore fail
before reaching byte limits. SQLite services may remain online; no live-file
copy, checkpoint, vacuum or migration is performed to create a backup.

Publication first writes destination-local temp files and then atomically renames
each final file without replacement. A complete backup requires **both** the final
archive and matching sibling `<archive>.sha256`, with full verification passing.
There is no atomic transaction spanning the pair. Interruption may leave private
temp files or half a pair; these are never listed as verified. A kernel-stalled
worker may remain kill-pending and leave private scratch. Treat a timed-out attempt
as unconfirmed and explicitly verify any resulting final pair before relying on
it. The command never cleans older archives or retries at another destination.

List examines at most 1000 direct entries, including foreign entries in that bound;
it never recurses or opens foreign filenames. An overfull directory fails visibly.
Verification checks the external SHA-256 first, all fixed archive structure,
manifest/member hashes, then the snapshot's application ID, packaged Alembic head,
WAL/quick/FK integrity. Repeated list/verify do not change destination contents.
The immutable returned identity includes the exact final hash and filename for
future recovery checks; it does not authorize restore, migration or rollback.
Scheduling/retention/status (#159), restore (#160) and update (#144) are separate.
