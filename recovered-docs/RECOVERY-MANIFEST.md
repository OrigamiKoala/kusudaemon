# Recovered markdown docs

Recovered 2026-09-06 from the kusudaemon git object store. Each file is the
**last committed version before it was deleted**, verified to be the newest
version of that path across every ref (149 commits, 18 local branches, origin
and upstream), not just `main`'s first-parent history.

| File | Lines | Bytes | Created | Deleted in |
|---|---|---|---|---|
| `PLAN.md` | 1456 | 93747 | 2026-08-07 | `7e45b8a` 2026-08-13 "make more efficient, squash some bugs" |
| `PLAN-AUDIT.md` | 912 | 50699 | 2026-08-12 | `7e45b8a` 2026-08-13 |
| `DASHBOARD-UX.md` | 693 | 36972 | 2026-08-11 | `7e45b8a` 2026-08-13 |
| `PLAN-zeromem.md` | 2196 | 106830 | 2026-08-09 | `d8a7dd2` 2026-08-09 "remove old stuff" |
| `AUDIT-2026-08-09.md` | 374 | 17899 | 2026-08-09 | `d8a7dd2` 2026-08-09 |
| `BACKEND-PARITY-AUDIT.md` | 372 | 25475 | 2026-08-14 | `5857275` 2026-08-15 "remove extra artifacts" |
| `ROLE-CALLS-VIA-BACKENDS-PLAN.md` | 376 | 22522 | 2026-08-14 | `5857275` 2026-08-15 |
| `PLAN-EFFICIENCY-AND-HORIZON.md` | 1182 | 57657 | 2026-08-15 | `1040772` 2026-08-15 "add antigravity adapter" |
| `PLAN-COST-AUDIT-2026-08.md` | 438 | 23566 | 2026-08-16 | `0b169b6` 2026-08-16 "remove" |
| `README.zh-CN.md` | 471 | 23955 | 2026-08-04 | `bd311fc` 2026-08-09 "Rename lh-harness -> Waypoint" |
| `out_unit-01.md` (was `out/unit-01.md`) | 3 | 38 | 2026-08-14 | `1040772` 2026-08-15 |

Path separators were flattened to `_` so everything sits in one folder;
`out_unit-01.md` was originally `out/unit-01.md`.

## Also searched, nothing further found

- **Unreachable objects** (`git fsck --unreachable`): 6 dropped stash commits
  from 2026-08-11/12 and 7 loose blobs. All contained only older or
  merge-conflicted copies of `PLAN.md`, `DASHBOARD-UX.md`, `CLAUDE.md`,
  `README.md` — no filename not already listed above.
- **Stale worktree** `.claude/worktrees/web-interface` (points at a `.git` in
  the old `LongHorizon-Harness` checkout): its `PLAN.md` is byte-identical to
  a committed historical version, so nothing unique.
- **Vendored subtrees** under `eval/OSWorldv2-harness/` and
  `eval/WeaveBench-harness/`: 92 upstream `.md` files were removed in
  `e036318` (2026-08-09, "oops") and `bd311fc`. These are third-party repo
  docs, not authored docs, so they were not restored — they are still in git
  history if wanted.
