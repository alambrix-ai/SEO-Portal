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

## Later phases (linear chain off the merge)

| Order | Feature branch | Migration revision |
|------:|----------------|--------------------|
| 6 | `feature/topic-cluster-planner` | `0026_topic_clusters` |
| 7 | `feature/ask-willy-grounding` | `0027_willy_grounding` |
| 8 | `feature/weekly-autopilot-report` | `0028_scheduled_reports` |
| 9 | `feature/anomaly-alerts` | `0029_alert_rules` |
| 10 | `feature/programmatic-seo` | `0030_pseo_pages` |
| 11 | `feature/product-feed-optimizer` | `0031_product_feed` |
| 12 | `feature/content-repurposer` | `0032_content_repurpose` |
| 13 | `feature/creative-studio-expansion` | `0033_creative_video_scripts` |

Each revises the previous head. Do not create another sibling of `0024_*`.
