---
version: 1
slug: "src-kusudaemon-dashboard-static-index-html"
primary_target: "src/kusudaemon/dashboard/static/index.html"
related_targets: []
---

# Dashboard (Operate)

Scope: the whole kusudaemon dashboard SPA. Mode: Operate. Replaces `dashboard/static/` (old look is anti-reference).

Audience and job: one operator watching/steering long, parallel, decomposed runs next to an editor, mostly in a dim room. Needs: state at a glance, every CLI verb, thinking + tool results + diffs, cost, multi-run.

Constraints: no explanatory copy; every string is a label, value or error. Keyboard-complete. Svelte 5 + Vite, build committed to static/.

Memorable moment: the Score, a timeline where every node is a staff line and each writer/review episode is a bar, live "now" edge.

## Direction contract

THESIS: A run is a score, not a chat. Parallel parts read left to right in time; the feed is one part zoomed. Refuses the chat-feed-plus-sidebar default of agent harnesses.
OWN-WORLD: Graphite ground (cool-tinted, oklch ~0.17), paper-light alternate. Hairline 1px rules at low alpha are the only structure: no cards, no shadows except popovers. One accent, brass (oklch ~0.80 0.10 85), reserved for the live/active thing. Status inks: sage done, vermilion failed, pale blue waiting-on-operator. Geist Sans for labels, Geist Mono tabular for every number, id and code. Drawn line icons, one stroke weight.
STORY: Operator sees phase, tier, spend and what needs them in the top bar, the whole run's shape in the Score, and drops into any part to read its thinking, tool calls and diffs; acts through the palette or the composer.
FIRST VIEWPORT: Left 248px rail: runs then node tree. Centre: tab strip of open runs, a hairline phase strip, then the Score filling the width (node rows, episode bars, brass now-edge) over the feed of the selected part. Right 400px inspector with icon tabs (Node, Changes, Artifact, Files, Docs, Cost, Events). Bottom: one-line composer; Cmd-K palette.
FORM: Score-based operator console (own list candidate 4: orchestral score). Seed key 5be2d8fa.
FINISH: unreviewed and unshipped until the finish reviewer runs.
