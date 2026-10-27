---
name: EleSync
description: Dark-only mission console for a local-first AI project workspace.
colors:
  void: "#060817"
  hull: "#0b0e22"
  hull-raised: "#121633"
  hull-high: "#1b2046"
  hull-violet: "#191541"
  seam: "#252b52"
  seam-strong: "#394077"
  signal-violet: "#a07cf0"
  signal-violet-deep: "#6d48d8"
  signal-violet-soft: "#c9b8ff"
  telemetry-cyan: "#5cd3ea"
  readout: "#eeeefa"
  readout-dim: "#a9aed0"
  readout-faint: "#8a90b8"
  status-go: "#45d69a"
  status-hold: "#ecc15a"
  status-fault: "#ec6f86"
typography:
  display:
    fontFamily: "Geist, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "28px"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-1px"
  headline:
    fontFamily: "Geist, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "22px"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.6px"
  title:
    fontFamily: "Geist, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "14px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "-0.1px"
  body:
    fontFamily: "Geist, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "Geist Mono, ui-monospace, 'Cascadia Mono', Consolas, monospace"
    fontSize: "12px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "1.45px"
  numeral:
    fontFamily: "Geist Mono, ui-monospace, 'Cascadia Mono', Consolas, monospace"
    fontSize: "14px"
    fontWeight: 700
    lineHeight: 1
rounded:
  sm: "6px"
  md: "10px"
  lg: "16px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  xxl: "24px"
components:
  button:
    backgroundColor: "{colors.hull}"
    textColor: "{colors.readout}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
    typography: "{typography.body}"
  button-hover:
    textColor: "{colors.signal-violet}"
  button-primary:
    backgroundColor: "{colors.signal-violet-deep}"
    textColor: "{colors.readout}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  button-danger:
    backgroundColor: "{colors.hull}"
    textColor: "{colors.status-fault}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  button-sm:
    padding: "5px 12px"
  card:
    backgroundColor: "{colors.hull}"
    rounded: "{rounded.md}"
    padding: "18px"
  input:
    backgroundColor: "{colors.hull-raised}"
    textColor: "{colors.readout}"
    rounded: "{rounded.sm}"
    padding: "9px 12px"
  nav-item:
    textColor: "{colors.readout-dim}"
    rounded: "{rounded.sm}"
    padding: "9px 18px"
  nav-item-active:
    textColor: "{colors.signal-violet}"
  status-chip:
    backgroundColor: "{colors.hull-high}"
    textColor: "{colors.readout-faint}"
    rounded: "{rounded.md}"
    padding: "2px 7px"
  tab-bar-mobile:
    backgroundColor: "{colors.void}"
    height: "64px"
---

# Design System: EleSync

## Overview

**Creative North Star: "The Mission Console"**

EleSync is the console the user runs their AI crew from. Every assistant (ChatGPT, Claude, Gemini, Grok and the rest) is a station reporting in, every project is a mission with its own boundary, and the dashboard is where the operator sees, at a glance, what is live, what is waiting, and what crossed a line. The room is dark on purpose. Deep indigo hulls recede so that the only things that light up are things worth reading: a status, an active project, a focused control, the elephant.

The console is **precise and quiet**. Density is real (12–14px working type, compact rows, mono numerals for counts), but nothing shouts. Borders are hairlines, corners are small, and surfaces sit flat as tonal layers. Light is a signal, not a decoration: violet means "this is yours / this is selected", cyan means "this is live / this is telemetry", and the green, amber and red status colours mean exactly what they say. The one place the console is allowed to perform is the active-project hero, where the elephant floats in its cyan-violet glow. It is the station's insignia, not wallpaper.

The UI is dark-only and treats the phone as a full console, not a port: the same mission state, compressed into a bottom tab bar and single-column panels, with real safe-area handling.

**Key Characteristics:**
- Dark-only indigo hull (`void` → `hull` → `hull-raised` → `hull-high`) with hairline `seam` borders.
- One accent (`signal-violet`) for ownership and selection; `telemetry-cyan` reserved for live state, info, focus rings and data.
- Geist for reading, Geist Mono for eyebrow labels and counts. Both are self-hosted and work offline.
- Flat tonal layering at rest; glow appears only as a response to state.
- Small radii (6 / 10 / 16px), compact padding, mission-status chips in uppercase.
- Phosphor line icons (SVG sprite, `currentColor`) everywhere. No emoji glyphs.
- Phone layout: fixed bottom tab bar at ≤700px with `env(safe-area-inset-*)` padding.

## Colors

A deep indigo night with two lights on the panel (violet for what is yours, cyan for what is live) and three status lamps.

### Primary
- **Signal Violet** (`signal-violet`): the single accent. Active nav item text, links, the selected project's border, focused input borders, the primary button, stat values. It answers "what am I operating on?"
- **Deep Signal Violet** (`signal-violet-deep`): the pressed, saturated end of the accent. The primary button's fill and the base of the active-project hero gradient.
- **Soft Signal Violet** (`signal-violet-soft`): the pale tint for manual-source tags and low-emphasis violet text on dark fills.

### Secondary
- **Telemetry Cyan** (`telemetry-cyan`): live and informational state only. Mono eyebrow labels, project tab underlines, counts, focus outlines (`2px solid`, `3px` offset), the left rule on import-source cards, and the "live" pulse.

### Tertiary (status lamps)
- **Status Go** (`status-go`): connected, active, saved. The `dot.on` lamp, the `status-active` chip and success toasts (with near-black text).
- **Status Hold** (`status-hold`): paused, offline, warnings. The `status-paused` chip, the offline indicator and warning badges.
- **Status Fault** (`status-fault`): errors and destructive actions. Danger button outlines, connect errors and error toasts.

### Neutral
- **Void** (`void`): the page background behind everything. It carries two faint radial washes (violet top-right, cyan bottom-left) that give the room depth without adding a surface.
- **Hull** (`hull`): base surface for the sidebar, cards, dialogs, buttons and search fields.
- **Raised Hull** (`hull-raised`): second tier for inputs, nested panels, hover fill on nav items and project rows.
- **High Hull** (`hull-high`): third tier for badges, progress tracks and archived chips.
- **Violet Hull** (`hull-violet`): a tinted surface for accent-adjacent stat cards.
- **Seam** / **Strong Seam** (`seam`, `seam-strong`): 1px borders and dividers; strong seam on hover of neutral elements.
- **Readout** / **Dim Readout** / **Faint Readout** (`readout`, `readout-dim`, `readout-faint`): primary text, secondary text and metadata/placeholder. Faint readout is the floor; never go dimmer for text.

### Brand-source colours
Each connected assistant keeps its own identity colour (`--chatgpt #10a37f`, `--claude #d4a27a`, `--gemini #4285f4`, `--grok #ff68c9`, `--deepseek #22d3ee`, `--perplexity`, `--copilot #6b93f2`, `--telegram #2aabee`, WhatsApp `#25d366`). They appear only as a 3px left rule, a dot, or a source tag, never as a fill or a button colour.

### Named Rules
**The Two Lights Rule.** Violet means *yours / selected*; cyan means *live / telemetry / focus*. Never swap them, and never introduce a third accent hue for decoration.

**The Lamp Rule.** Green, amber and red are status lamps and only ever report state. A green button that isn't confirming success is a lie.

**The Token-Only Rule.** Colour comes from the `:root` custom properties. For a tint, use the channel tokens (`--accent-rgb`, `--accent2-rgb`, `--success-rgb`, `--warn-rgb`, `--danger-rgb`, `--bg-rgb`, `--surface-rgb`) as `rgb(var(--accent-rgb) / .16)`, never a literal `rgba()`. The old neon literals (`rgba(192,77,255,…)`, `rgba(40,231,255,…)`) are retired.

## Typography

**Display Font:** Geist (with -apple-system, BlinkMacSystemFont, Segoe UI)
**Body Font:** Geist
**Label/Mono Font:** Geist Mono (with ui-monospace, Cascadia Mono, Consolas)

**Character:** Geist is the console's voice: neutral, engineered, tight at large sizes. Geist Mono is the instrument read-out, used for eyebrow labels and numbers so that figures line up and feel measured. Both are self-hosted variable fonts (`/static/*.woff2`, SIL OFL) with no third-party request, so they work offline and in the PWA cache.

### Hierarchy
- **Display** (700, 28px, line-height 1, −1px): the page greeting on the command center (`cc-header h1`); 24px on phones.
- **Headline** (700, 22–24px, −0.6 to −0.7px): page intros and the active-project title. The import hero may scale to `clamp(22px, 3vw, 32px)`.
- **Title** (600, 14px, −0.1px): card titles and dashboard card headings.
- **Body** (400–500, 13px, line-height 1.6): working copy, buttons, inputs, nav. Root size is 14px; 12px is the floor for any visible text.
- **Label** (Geist Mono 700, 12px, +1.45px tracking, uppercase): cyan eyebrows above page titles and dashboard sections. Status chips and detail labels use Geist 600, 12px, uppercase, +0.3 to 0.5px.
- **Numeral** (Geist Mono 700, 14–28px): counts, stat values and project memory totals.

### Named Rules
**The 12px Floor Rule.** No visible text below 12px, including badges, metadata and tab-bar labels.

**The Instrument Rule.** Numbers that the user compares (counts, totals, metrics) are set in Geist Mono so they read like gauges, not prose.

## Layout

The desktop is a fixed **188px sidebar** (178px on the command center) plus a scrolling main column. The topbar is 52px tall with a faint violet→cyan wash, and content padding is `20px 24px 40px`. The command center caps at **1180px** and centres itself. It is built on a named-area grid: two flexible columns plus a 182px stats column, with areas `"active active stats" / "collaborators imports activity" / "knowledge knowledge activity"`, a 10px gap, and a Companion bar beneath.

Spacing runs on a 4px base, with 8 / 10 / 12 / 16 / 18 / 20 / 24px as the working steps. Card padding is 18px and dense command-center cards drop to 13px. Grids use 10–12px gaps.

**Breakpoints:**
- **≤1180px / ≤1050px**: the command grid collapses to one flexible column plus a 172px stats column; activity drops below.
- **≤900px**: tighter topbar.
- **≤700px (or a standalone PWA with coarse pointer)**: the sidebar becomes a **fixed bottom tab bar** (64–65px + `env(safe-area-inset-bottom)`, 97% opaque void with blur). Brand, footer and secondary nav are hidden. Content padding-bottom clears the bar. Grids go single-column, the search field hides, the primary action goes full-width, and the detail panel becomes a full-screen sheet with safe-area padding and 44px close targets.
- **≤480px**: final compaction.

Hover effects are gated behind `@media (hover:hover) and (pointer:fine)` so touch devices never get stuck hover states.

### Named Rules
**The Phone Is a Console Rule.** Every surface must be fully operable at 393px wide inside the bottom-tab layout, with safe-area insets respected. A desktop-only control is a bug.

## Elevation & Depth

Depth is **tonal**, not cast. Surfaces step up from `void` to `hull`, `hull-raised` and `hull-high`, separated by 1px `seam` hairlines. At rest, panels are flat. Light enters only as a response to state: hover, focus, the selected item, a live signal. Cards, stat tiles, the knowledge and Companion bars, memory cards and the import hero are all flat hull surfaces. The single exception is the active-project hero.

### Shadow Vocabulary
- **Glow ring, violet** (`box-shadow: 0 0 0 1px rgba(160,124,240,0.35)`, `--glow`): hover and selection on neutral controls and cards.
- **Glow ring, cyan** (`box-shadow: 0 0 0 1px rgba(92,211,234,0.3)`, `--glow-cyan`): hover on live and navigational items (project rows, import sources).
- **Overlay lift** (`box-shadow: 0 8px 32px rgba(0,0,0,0.5)`): dialogs only, over a `rgba(0,0,0,.5)` scrim.
- **Ambient** (`box-shadow: 0 2px 16px rgba(3,4,18,0.45)`, `--shadow`): floating layers only (toasts, the sticky bulk-action bar). Never on a resting card.
- **Hero bloom**: the active-project card alone combines radial cyan and violet light with the floating elephant (`cc-elephant-float`, 4.8s ease-in-out, disabled under `prefers-reduced-motion`).

### Named Rules
**The Glow-Is-State Rule.** A glow, ring or lift must answer "what changed?" (hovered, focused, selected, live). If nothing changed, the surface is flat.

**The One Bloom Rule.** Only the active-project hero gets gradient light and the animated elephant. Every other card is a flat hull.

## Shapes

The shapes are small, engineered and consistent: **6px** (`rounded.sm`) for controls (buttons, inputs, nav items, toasts), **10px** (`rounded.md`) for cards, dialogs, chips and project marks, and **16px** (`rounded.lg`) only for large hero panels. Borders are 1px hairlines everywhere. A **3px left rule** is the one heavier stroke, used to tag a row with a source or client colour, and a **2px bottom rule** marks the active project tab. Status dots are 7px circles, and source icons sit in 32px circles. Project marks are 34–36px rounded squares with a violet→cyan gradient and the project initial.

## Components

### Buttons
Precise and quiet: hairline, compact, with only the primary allowed to carry light.
- **Shape:** gently squared (6px).
- **Default:** hull fill, seam border, readout text, `8px 16px`, 13px/500, icon gap 6px.
- **Primary:** violet fill (`--accent` → `--accent-strong`, 135°) with readout text and no border. It is the one button per view that may carry a soft violet glow.
- **Danger:** fault-red outline and text; on hover, a 10% fault fill.
- **Small:** `5px 12px`, 12px.
- **Hover / Focus:** on fine pointers, the border and text shift to violet with the violet glow ring. Primary hover adds a −1px lift. Focus-visible is always a 2px cyan outline with 3px offset. Disabled is 50% opacity with no motion.
- **Text button:** borderless cyan 12px/600 for inline secondary actions.

### Chips (status)
- **Style:** 12px uppercase, `2px 7px`, 10px radius, with lamp-coloured text on a ~10% tint of the same lamp (`active` green, `paused` amber, `archived` faint on high hull).
- **Nav badge:** 12px/600 count on high hull; warning variant in amber.

### Cards / Containers
- **Corner Style:** 10px.
- **Background:** hull, with nested blocks on raised hull.
- **Shadow Strategy:** flat at rest. Hover on fine pointers shows the violet glow ring (see Elevation).
- **Border:** 1px seam.
- **Internal Padding:** 18px standard, 13px in the dense command-center grid.

### Inputs / Fields
- **Style:** raised-hull fill (search fields use hull), 1px seam, 6px radius, `9px 12–14px`, 13px readout text, faint-readout placeholder.
- **Focus:** the border turns signal violet with no outer glow, plus a cyan focus-visible outline from the global rule.
- **Labels:** 12px/600 uppercase dim readout, +0.5px, 6px above the field.
- **Checkboxes:** `accent-color: var(--accent2)`.

### Navigation
- **Desktop sidebar:** hull, 188px, with the elephant lockup at the top. Items are 13px/500 dim readout with 16px Phosphor icons. Hover uses a raised-hull fill. The active item gets a violet-tinted fill (`--accent-bg`) with violet text; on the command center it adds a 3px inset violet rule. Advanced actions sit in a native `<details>` "More" group.
- **Project tabs:** 12px/600 text tabs; active or hover shows cyan text with a 2px cyan underline. They wrap rather than scroll on phones.
- **Mobile:** a fixed bottom tab bar with icon over a 12px label, evenly spaced, safe-area padded, over a blurred near-opaque void.

### Project Mission Card (signature)
The active-project hero on the command center. It is a 184px-minimum card with a violet border and the only gradient bloom in the system. The project title and actions sit on the left (≤60% width) and the elephant floats on the right with a radial mask and cyan/violet drop-shadow. When there is no project it shows an empty state in the same frame. This is the console's insignia. Don't replicate it elsewhere.

### Project Row
A clickable project in lists and the command-center strip. It is a raised-hull row with a 34px gradient initial mark, a truncated name and description, and a cyan mono count on the right. On hover with a fine pointer it gets a cyan border, the cyan glow ring and a 2px rightward nudge.

### Toast
Bottom-right, 6px radius, 13px/500. Success is green with near-black text and errors are red with readout text. It enters with a 0.3s fade-and-rise.

## Do's and Don'ts

### Do:
- **Do** pull every colour, radius and font from the `:root` custom properties in `elesync/static/dashboard.css`, and mirror any token change into `login.css`, which redeclares the palette.
- **Do** keep violet for selection and ownership and cyan for live state, info and focus (The Two Lights Rule).
- **Do** keep surfaces flat at rest and add glow only for hover, focus, selection or live state (The Glow-Is-State Rule).
- **Do** set compared numbers in Geist Mono and eyebrows in Geist Mono 12px uppercase cyan.
- **Do** gate hover effects behind `(hover:hover) and (pointer:fine)` and honour `prefers-reduced-motion`.
- **Do** use Phosphor icons from the inline SVG sprite (`<svg class="ph"><use href="#ph-NAME"/></svg>`), inheriting `currentColor`.
- **Do** use the z-index scale tokens (`--z-raised` … `--z-toast`), never bare numbers.
- **Do** design every surface for the ≤700px bottom-tab layout with safe-area insets and 44px touch targets.

### Don't:
- **Don't** add a light theme or light surfaces; EleSync is dark-only.
- **Don't** use emoji or Unicode glyphs as icons.
- **Don't** write literal `rgba()` tints of palette colours; use `rgb(var(--accent-rgb) / α)` and the other channel tokens.
- **Don't** give ordinary cards gradient fills, resting shadows or the elephant. The bloom belongs to the active-project hero only (The One Bloom Rule).
- **Don't** use status green, amber or red for anything but status.
- **Don't** set visible text below 12px.
- **Don't** fill buttons or surfaces with an assistant's brand colour; brand colours are rules, dots and tags only.
- **Don't** use the elephant artwork in any form other than the supplied official asset (`elesync-main-logo.webp` / `assets/elesync-official-logo.png`). The one approved exception is the Cosmic theme's gold copy (`elesync-main-logo-cosmic.webp/.png`, `elesync-favicon-cosmic.png`), generated from the official asset by `scripts/build_cosmic_icons.py`. Regenerate it with that script, never edit it by hand, and never recolour the elephant with a CSS hue filter.
