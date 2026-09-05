# Display hardware guidance

PostcardScene treats the attached television or monitor primarily as an HDMI-connected display panel. Application logic, media selection, composition, scheduling, and most processing belong to PostcardScene and its Linux host rather than to the display's smart-TV platform.

This document records the display characteristics that matter to the project, the compatibility evidence that must eventually be collected, and current candidate hardware. It is not a purchasing guarantee or a list of officially supported displays.

## Target display profile

PostcardScene does not require a particular brand, smart-TV operating system, or panel size.

For photography-oriented installations, a useful target is:

- 4K UHD (`3840x2160`) where practical; 1080p remains the minimum V0 target;
- 60 Hz is sufficient for normal PostcardScene use;
- LCD/QLED/Mini-LED panels are suitable choices for unattended mixed media and static-content use;
- good colour reproduction, useful brightness, contrast, and panel uniformity matter more than television processing features;
- low-reflection or anti-glare treatment is desirable, especially in bright rooms;
- sufficiently wide viewing angles are desirable for wall-mounted ambient displays viewed from several positions;
- narrow bezels and VESA mounting are desirable for picture-frame-style installations;
- HDMI audio is useful where video sound is enabled.

A 55-inch 4K television currently represents an attractive price/size class for a primary PostcardScene installation, but size is an installation choice rather than a project requirement.

## Compatibility requirements and evidence

A promising specification sheet is not sufficient evidence that a display is compatible with every PostcardScene appliance function.

Relevant characteristics include:

- reliable 4K60 HDMI input where 4K is intended;
- EDID and mode reporting that Linux can consume predictably;
- HDMI-CEC support where CEC is used for standby/wake control;
- predictable CEC standby, wake, and power-state behaviour with the actual Linux/Raspberry Pi stack;
- HDMI reconnect/hotplug behaviour without requiring a host reboot;
- predictable HDMI audio behaviour where sound is required;
- ability to disable or configure television-side automatic sleep, source switching, image processing, and other behaviours that conflict with appliance operation;
- VESA mounting and physical clearances appropriate to the installation.

Advertised HDMI-CEC support must not be treated as proof that PostcardScene can reliably power a specific display on/off. CEC, DDC/CI, DRM/KMS signal control, graphics-session behaviour, 4K output, audio, and hotplug behaviour require representative physical validation where the corresponding support claim is made.

Issue #31 owns the primary Linux graphics/session and 4K hardware evidence. Issue #10 owns physical display-power behaviour and its supported backends.

## Image-display qualities

For a display used substantially as a digital photography frame, prefer spending budget on characteristics that affect the displayed image directly:

- colour reproduction and a usable neutral/picture mode;
- brightness appropriate to the room;
- reflection handling;
- contrast and black-level behaviour;
- panel uniformity;
- viewing-angle behaviour;
- native 4K presentation of high-resolution source material.

Premium gaming refresh rates, VRR, tuner features, smart-TV application performance, AI television features, and other television-platform extras are not PostcardScene requirements. They may be present on a suitable panel but should not drive the selection unless the installation has another use for them.

OLED is not excluded, but its cost and static-content considerations should be weighed against the intended unattended-display workload. PostcardScene includes panel-protection responsibilities regardless of panel technology.

## Current candidate displays

The following models are **candidate displays only**. They have been identified during project planning because their advertised panel characteristics and current market positioning appear close to the desired PostcardScene profile.

They have **not** been validated as PostcardScene-compatible hardware and must not be described as supported, recommended from compatibility evidence, or known-good until representative physical testing is completed.

Market observations below are a snapshot from **2026-09-05 in Greece** and are expected to change.

| Candidate | Planning role | Why it is being considered | Status |
| --- | --- | --- | --- |
| Xiaomi TV S Pro Mini LED 55 (2026) | Preferred reference candidate | 55-inch 4K Mini-LED class panel, strong brightness/contrast potential, manufacturer-advertised low-reflection treatment, narrow-bezel design, and HDMI-CEC capability; observed around the EUR 500 class during planning | Candidate only; not physically validated |
| Xiaomi TV A Pro 55 (2026) | Budget reference candidate | 55-inch 4K QLED class panel, narrow-bezel design and HDMI-CEC capability at a substantially lower observed price, around the EUR 370 class during planning | Candidate only; not physically validated |

These models are not dependencies of the project. Other televisions and monitors may be equally or more suitable when they meet the required display and control characteristics.

### Candidate validation checklist

If either candidate, or another display, becomes reference hardware, record at least:

- exact model and relevant firmware version;
- Raspberry Pi/host model and OS/kernel/graphics stack;
- successful 1080p and intended 4K resolution/refresh operation;
- Chromium and mpv targeting the intended display;
- HDMI-CEC standby, wake, and reported-state behaviour where supported;
- HDMI disconnect/reconnect behaviour;
- boot-without-display and later-connect recovery;
- HDMI audio behaviour where applicable;
- observed hardware acceleration/video decode state;
- obvious brightness, reflection, colour, viewing-angle, or scaling limitations relevant to normal PostcardScene use.

Only after such evidence exists should a candidate move into a tested/known-compatible hardware record.

## Hardware-status terminology

Use hardware labels conservatively:

- **Candidate** — appears promising from specifications, market research, or intended requirements but has not been physically validated with PostcardScene.
- **Tested** — exercised on a documented PostcardScene/Linux configuration with recorded evidence and limitations.
- **Known compatible** — tested evidence covers the specific capabilities being claimed; this does not imply every feature, firmware version, host, cable, resolution, or Linux distribution is universally supported.

When evidence is incomplete, retain the narrower status rather than infer compatibility from brand, HDMI version, or an advertised CEC logo.
