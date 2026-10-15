---
name: Kusudaemon
description: A score-based operator console for long, parallel, decomposed agent runs.
colors:
  graphite: "oklch(0.165 0.006 265)"
  graphite-raised: "oklch(0.192 0.006 265)"
  graphite-hover: "oklch(0.228 0.007 265)"
  graphite-active: "oklch(0.27 0.008 265)"
  ink: "oklch(0.935 0.004 265)"
  ink-secondary: "oklch(0.76 0.006 265)"
  ink-tertiary: "oklch(0.66 0.008 265)"
  brass: "oklch(0.8 0.1 85)"
  sage: "oklch(0.78 0.1 155)"
  vermilion: "oklch(0.72 0.15 25)"
  slate-blue: "oklch(0.78 0.09 245)"
  amber: "oklch(0.78 0.12 62)"
  paper: "oklch(0.985 0.002 265)"
  paper-ink: "oklch(0.2 0.01 265)"
  paper-brass: "oklch(0.52 0.11 75)"
typography:
  ui:
    fontFamily: "Hanken Grotesk Variable, ui-sans-serif, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: "Hanken Grotesk Variable, ui-sans-serif, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 560
  data:
    fontFamily: "Spline Sans Mono Variable, ui-monospace, Menlo, monospace"
    fontSize: "12px"
    fontWeight: 400
    letterSpacing: "-0.01em"
rounded:
  sm: "3px"
  md: "6px"
  pill: "9px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  button:
    backgroundColor: "transparent"
    textColor: "{colors.ink-secondary}"
    rounded: "{rounded.sm}"
    height: "26px"
    padding: "0 10px"
  button-primary:
    backgroundColor: "{colors.brass}"
    textColor: "{colors.graphite}"
    rounded: "{rounded.sm}"
    height: "26px"
  icon-button:
    textColor: "{colors.ink-tertiary}"
    rounded: "{rounded.sm}"
    size: "26px"
  input:
    backgroundColor: "{colors.graphite-hover}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "5px 8px"
  chip:
    textColor: "{colors.ink-secondary}"
    rounded: "{rounded.pill}"
    height: "18px"
    padding: "0 6px"
---

# Design System: Kusudaemon

## Overview

A run is a score, not a chat. Parallel parts (nodes) read left to right in time; the feed is one part zoomed in. The interface is a quiet instrument: graphite ground, hairline rules, one brass accent reserved for whatever is live, and mono numerals for every measurement. Density is welcome, noise is not. No visible string explains itself: every word on screen is a label, an id, a value or an error.

Dark graphite is the default because the operator watches runs for hours in a dim room beside an editor. Paper light follows `prefers-color-scheme` or the theme toggle and uses the same structure with inverted values.

## Colors

- **Graphite** (`graphite`, `graphite-raised`, `graphite-hover`, `graphite-active`) is the only neutral ramp, tinted cool (hue 265). Surfaces step up in lightness, never in saturation: rail and inspector sit on `graphite-raised`, hover on `graphite-hover`, selected rows on `graphite-active`.
- **Brass** is the accent and means *live or selected*: the running episode, the now-edge, the focus ring, the primary button, the selected row tint. It is never used for decoration.
- **Status inks** pair shape with colour: `sage` passed/added, `vermilion` failed/removed, `slate-blue` waiting on the operator, `amber` blocked/warning. Text in these inks stays at or above 4.5:1 on graphite.
- `ink-tertiary` is the floor for text (5:1 on the base surface, 4.8:1 on `graphite-active`).
- Light theme keeps the roles: `paper` ground, `paper-ink` text, a darker `paper-brass`.

## Typography

Hanken Grotesk carries labels and prose at 12–13px; Spline Sans Mono carries ids, paths, numbers, code and diffs, with tabular figures. Hierarchy comes from weight (400 / 560 / 620) and colour step (`ink`, `ink-secondary`, `ink-tertiary`), not from size: the only larger size is the wordmark (15px) and the token total in the usage tab (26px). Prose measure is capped at 78ch.

## Layout

Three columns on a 252px rail, a fluid centre and a 420px inspector (both drag-resizable, both collapsible). The centre stacks run tabs, run bar, waiting tray, Score and Feed, composer. Separation is by 1px hairlines (`--line-soft`), never by cards or boxes; spacing steps are 4 / 8 / 12 / 16 / 24. Below 980px the rail and inspector become drawers over a scrim.

## Elevation & Depth

Flat. The only shadows belong to things that float: palette, toasts, tooltips and drawers use one soft two-layer shadow. Overlays use a tinted scrim, not blur.

## Shapes

3px radius on controls, 6px on contained blocks (diff files, output blocks, popovers), 9px on chips. Status dots change shape as well as colour: dotted ring pending, filled disc passed, diamond failed, square blocked, half-filled in review, dashed split/stale.

## Components

- **Score** — node lines over time. Bars are episodes (brass breathing = running, sage = done, vermilion = error, hatched vermilion = timeout), 2px ticks are events, the top lane is phases, a 1px brass line is *now*.
- **Feed items** — thinking (dim, clamped to three lines until opened), tool rows (one line: kind icon, name, mono title, +/- counts, state; expand for input, diff, output), assistant markdown, hairline-framed system separators, slim usage lines.
- **Diff** — unified or split, line numbers in the gutter when real, word-level marks on paired changes, syntax-highlighted.
- **Palette** — one fuzzy list over commands, nodes and runs; multi-step arguments; shows the CLI equivalent.
- **Approval card** — the only tinted block (faint slate-blue wash while pending).

## Do's and Don'ts

- Do keep a status colour paired with a shape or a word.
- Do use mono for anything the operator might copy.
- Do let brass appear only where something is live or selected.
- Don't add explanatory text, empty-state prose or tooltips that restate a label; use an icon with an aria-label.
- Don't nest containers or add card chrome; use a hairline.
- Don't use side-stripe borders, gradient text, glass, or decorative motion. Motion is a 140ms ease-out on state change plus the single breathing of the live bar.
