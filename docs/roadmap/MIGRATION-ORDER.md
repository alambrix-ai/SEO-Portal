# Alembic merge order (Phase 1–3b)

Four feature branches each introduced a migration that revises
`0023_audit_truncate_guard`, creating parallel heads:

| Order | Feature branch | Migration revision | Tables / purpose |
|------:|----------------|--------------------|------------------|
| 1 | `feature/ai-visibility-tracker` | `0024_ai_visibility` | Visibility queries / runs / observations |
| 2 | `feature/content-refresh-agent` | `0024_content_refresh` | `content_refresh_candidates` |
| 3 | `feature/auto-applied-fixes` | `0024_seo_issue_fix_proposals` | `seo_issue_fix_proposals` |
| 4 | `feature/internal-linking-agent` | `0024_internal_link_suggestions` | `internal_link_suggestions` |
| 5 | `feature/migration-merge` | `0025_merge_phase1_to_3b` | Empty merge of the four heads |

## Recommended GitHub merge order

1. Merge (or land) branches **1 → 4** above so application code and each
   `0024_*` migration reach `main` (or your integration branch).
2. Then merge **`feature/migration-merge`**, which adds
   `0025_merge_phase1_to_3b` (and, on this branch for a complete history
   graph, copies of the four `0024_*` files — treat those as identical to
   the feature PRs if Git reports a content conflict).
3. From then on, every new migration must set
   `down_revision = "0025_merge_phase1_to_3b"` (or the latest successor),
   with a unique ordered id (`0026_…`, `0027_…`, …). Do not invent another
   sibling of `0024_*`.

## Why this order

Visibility and content-refresh are independent product features; fix
proposals and internal linking also stand alone. Ordering them 1–4 is
only for a predictable review sequence — Alembic does not care about
Git order as long as all four `0024_*` revisions exist before the merge
revision runs.

## Later phases

Topic clusters, Willy grounding, scheduled reports, and alert rules chain
off `0025_merge_phase1_to_3b` (see their feature branches).
