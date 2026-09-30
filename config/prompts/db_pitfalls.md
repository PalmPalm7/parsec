## Provision Database: Column Pitfalls

**CRITICAL: Consult the "Reporting Database Reference" section before writing
SQL.** Do not guess column names — use ONLY columns listed in the schema
reference. If unsure, call `db_describe_table` to check. Common mistakes:

- `provisions` has NO `email` or `user_email` column — join `users u ON u.id = p.user_id`
  and select `u.email`. The requesting user's name is in `ordered_by`.
- `provisions` has NO `status` or `current_state` column — use `provision_result` and
  `last_state`.
- `provisions` has `catalog_id` (NOT `catalog_item_id`, NOT `catalog_item_name`) — join
  with `catalog_items` via `p.catalog_id = ci.id`
- `provisions` has both `updated_at` and `modified_at` — use `modified_at`
- `lifecycle_log` joins to provisions via `provision_uuid` (the provision's `uuid`, NOT
  the `babylon_guid`). It has `state`, `executor`, `logged_at` and `comments` — NO
  `action`, `event_type`, `message` or `timestamp`.
- `tower_job_log` and `provision_job` column names are camelCase and MUST be
  double-quoted: `"deployerJob"`, `"towerHost"`, `"towerJobURL"`, `"jobStatus"`.
  Unquoted, Postgres folds `deployerJob` to `deployerjob`, which does not exist.
- `tower_job_log` has NO `provision_uuid` and does not join to provisions. A
  provision's own AAP2 job is `provisions.tower_job_id` / `tower_job_url`; its
  per-action jobs are in `provision_job` (`pj.provision_uuid = p.uuid`, one row per
  `action`).
- `resource_claim_log` has no `id` column — its key is `provision_uuid`.
- A GUID that matches no `babylon_guid` may be the suffix of a ResourceClaim name:
  the GUID of the ordered catalog item, while the provision row carries its
  component's own GUID. Check it once before handing off to Babylon:
  `SELECT rcl.provision_uuid, rcl.resource_claim_name FROM resource_claim_log rcl
  WHERE rcl.resource_claim_name LIKE '%-<guid>'`, then join
  `provisions p ON p.uuid = rcl.provision_uuid`.
- `provision_cost` is partitioned — always include a `month_ts` filter to avoid full
  partition scans
- When joining tables with shared column names (e.g. `category`), always use table
  aliases to avoid ambiguous column errors
