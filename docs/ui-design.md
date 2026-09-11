# UI design

PostcardScene is a quiet, image-led display appliance. Photography, video and maps
provide the visual richness; the interface provides clear hierarchy and restraint.
This is the design authority for later UI work, not a component catalogue.

## Control interface

The Flask/Jinja control interface uses Bootstrap 5.3.x, pinned initially to 5.3.8,
for layout and component primitives. PostcardScene CSS loads after Bootstrap and
adapts its CSS custom properties through a small `--psc-*` semantic token layer.
Bootstrap is infrastructure, not the product identity.

Dark is the default, even when the browser's operating-system preference is light.
An explicit **Light theme** toggle uses Bootstrap's `data-bs-theme` mechanism; its
pressed state means light is selected. The browser stores this presentation-only
choice in `localStorage` under `postcardscene.control-theme`. Only `light` and
`dark` are meaningful; absent, invalid or unreadable preferences default to dark.
Clearing site storage resets the preference. Storage failure still permits a
page-local switch. With JavaScript disabled, the switch is hidden and the shell
remains usable in dark mode. Native modules apply saved preferences after parsing,
so a saved light preference may briefly show the dark default during loading.

The preference does not change application settings, server state, or projected
scene styling. Authentication and settings behavior belong to their owning issues.

Use charcoal/slate surfaces and one restrained cool accent in dark mode. Light
mode uses pale neutral backgrounds, opaque white surfaces and a darker cool accent.
Control forms, diagnostics, errors and tables should have predictable, mostly
opaque surfaces. Do not turn every control card into a translucent panel.

Current tokens in `web/static/control.css` cover body, surface, elevated surface,
primary/muted text, accent, focus, border and radius. Reuse these semantic roles
before introducing more tokens. Exact color values may evolve with real UI needs.
Bootstrap's spacing utilities are sufficient for the current shell; avoid a
parallel spacing system or speculative component classes.

## Projected scenes and information overlays

The display-facing UI shares the restrained design language but has a different
job. Images, video, maps and other content remain visually dominant. Do not project
the administration shell or make a scene look like a dashboard by default.

Text/information overlays normally use **dark translucent panels or scrims** with
strong light-text contrast, subtle separation/borders and modest corner radii.
Keep opacity only as strong as needed for legibility over the actual content.
Restrained blur via `backdrop-filter` is progressive enhancement: provide a
readable dark fallback when blur is unavailable. Check contrast against both light
and dark imagery; blur alone never guarantees readable text.

Overlay/scrim and overlay-text are future display token concepts, independent of
the control theme. Switching administration to light must not turn projected
information panels white. Later widgets/scenes may deliberately override this
baseline where their content requires it. This issue supplies design guidance,
not renderer code or speculative overlay components.

## Transient projected playback controls

#92 uses one compact bottom GTK3 layer-shell panel above image/video/web content,
with a charcoal translucent background, light text, visible labels and generous
button padding. Previous, Play/Pause and Next are always present when visible;
seek ±10 seconds appears only for seekable video, and mute/volume ±10 only with
audio capability. Play/Pause reflects logical pause; no filenames, URLs, scrubber
or permanent dashboard chrome is shown. Native button activation emits one typed
action; compositor keys provide focus-independent keyboard operation.

Controls start hidden. Local actions reveal/reset a fixed five-second timer;
pause and status refresh do not prevent auto-hide. A separate transparent
8-logical-pixel bottom-edge hotspot accepts pointer enter/click/touch only while
the main panel is hidden. Suppression disables both surfaces. Arbitrary pointer
motion over content does not reveal controls. Physical touch/keyboard/display
practicality remains unverified until the owning hardware evidence gates, and
RuntimeHost wiring remains #93.

## Typography, surfaces and interaction

- Use the local system sans-serif stack; no remote fonts are required.
- Favor a clear heading hierarchy, comfortable line height and restrained text
  widths. Larger overview headings may provide emphasis without decorative art.
- Use comfortable spacing, modest radii, subtle borders and minimal shadows.
- Reserve accent color for orientation and interaction. Do not rely on color alone
  for active, selected, error or focus states.
- Avoid gratuitous gradients, glass effects, ornament and animation. Any later
  motion must serve comprehension and respect reduced-motion preferences.

Keep visible labels, semantic landmarks, native keyboard controls, visible focus
and useful error feedback. Preserve the skip-to-content link and current-page
navigation semantics. Content must wrap without horizontal overflow on narrow
screens; controls should remain comfortably operable by touch. Verify desktop and
mobile widths, keyboard navigation and readable contrast in both themes whenever
material UI changes are introduced. The projected display requires its own
content/legibility validation; control-browser checks cannot prove HDMI support.

## Browser code and assets

Use modern vanilla JavaScript in readable `.js` files with native ES modules
(`<script type="module">`, browser-native `import`/`export` when needed). Keep DOM
wiring explicit and small, with no inline handlers or first-party global `window`
state. Separate pure reusable logic only when a real seam exists. Use JSDoc for
non-obvious contracts rather than annotating trivial functions.

TypeScript is a future option when substantial client state, typed API contracts,
reusable client logic or editor/drag-and-drop behavior makes its build cost
proportionate. A later explicit issue must own that decision.

Vendor official production/minified third-party assets locally under packaged
`web/static/vendor`. Bootstrap CSS and `bootstrap.bundle.min.js` are unmodified
upstream 5.3.8 distributions, with license and provenance alongside them. Runtime
pages require no CDN or internet access. See the [vendored notice](https://github.com/stef-k/PostcardScene/blob/main/src/postcardscene/web/static/vendor/bootstrap-5.3.8/README.md).

Serve first-party CSS/JS source directly, readable and unminified. Do not add
parallel generated `.min.*` files, source maps, precompressed `.gz`/`.br` variants,
concatenation, asset manifests or hashed/fingerprinted filenames. Normal browser
caching is sufficient now. Production compression and versioned/immutable caching
belong to #29/#26 when justified.

There is no Node/npm, TypeScript compiler, Sass/SCSS, bundler, transpiler, minifier,
tree-shaker, SPA/HTMX or frontend build/test pipeline. Compiled Bootstrap plus CSS
variables serves current needs. Reconsider build tooling only for measured asset
costs or concrete client complexity through an explicit later issue. Browser
visual verification can use temporary external tooling without introducing a
permanent project automation framework.

## Inspiration

These links are inspiration only, not templates or pixel specifications:

- <https://immich.app/>
- <https://prium.github.io/slick/v2.1.0/blocks/header-6.html>
- <https://dribbble.com/shots/10475643-AstroDigital-Website-Redesign-UI-UX-design>
- <https://dribbble.com/shots/27584916-Oil-Trading-Website-Hero-for-Premium-Fintech-Web-Design>
