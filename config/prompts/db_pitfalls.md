## Provision Database: Column Pitfalls

**CRITICAL: Consult the "Reporting Database Reference" section before writing
SQL.** Do not guess column names — use ONLY columns listed in the schema
reference. If unsure, call `db_describe_table` to check. Common mistakes:

- `provisions` has NO `email` or `user_email` column.
  p.ordered_by is the requester's email (FK to users.email; may be NULL). p.user_id → users is the assigned user and can differ.
  For the assigned user's email, join `users u ON u.id = p.user_id` and select `u.email`.
- `provisions` has NO `status` or `current_state` column — use `provision_result` and
  `last_state`.
- `provisions` has `catalog_id` (NOT `catalog_item_id`, NOT `catalog_item_name`) — join
  with `catalog_items` via `p.catalog_id = ci.id`
- `provisions` has both `updated_at` and `modified_at` — use `modified_at`
- `lifecycle_log` joins to provisions via `provision_uuid` (the provision's `uuid`, NOT
  the `babylon_guid`). It has `state`, `executor`, `logged_at` and `comments` — NO
  `action`, `event_type`, `message` or `timestamp`.
- `tower_job_log` and `provision_job` mix camelCase and snake_case column names. The
  camelCase ones MUST be double-quoted: `"deployerJob"`, `"towerHost"`, `"towerJobURL"`
  (both tables), and `"jobStatus"`, `"startTimestamp"`, `"completeTimestamp"`
  (`provision_job` only). Unquoted, Postgres folds `deployerJob` to `deployerjob`,
  which does not exist. Every other column in both tables is lowercase snake_case and
  is written unquoted — e.g. `provision_uuid`, `action`, `runtime_hour`,
  `extra_vars_json`, `created_at`.
- `tower_job_log` has NO `provision_uuid` and does not join to provisions.
  `provisions.tower_job_id` / `tower_job_url` is the `provision` action's job only —
  never the stop, start or destroy job. Every action's jobs are in `provision_job`
  (`pj.provision_uuid = p.uuid`, one row per action run: filter on `action` and take
  the newest `"startTimestamp"`).
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
