# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small suite of Python scripts around Jira Cloud data for the GOPER project:

- [main.py](main.py) — exports raw issues + full changelogs to a timestamped Excel workbook.
- [sprint_metrics.py](sprint_metrics.py) — computes the sprint metrics defined in the team's metrics spec (scope completion, scope deviation, blocked time, lead time, review time, rework rate) for a single sprint and exports them to Excel.
- [jira_client.py](jira_client.py) — shared Jira API helpers used by both scripts.
- [business_hours.py](business_hours.py) — business-hours (seg-sex 9h-18h, BR national holidays excluded) duration calculator, used only by `sprint_metrics.py`.

No package structure, test suite, or build step.

For the definition of each metric (formulas, scope classification, "entregue" criteria, methodology decisions, known limitations), see [METRICAS.md](METRICAS.md). Keep it in sync when changing metric logic in `sprint_metrics.py`.

## Running

```bash
python main.py            # export raw data
python sprint_metrics.py  # compute sprint metrics
```

Both prompt interactively at startup. `main.py` asks for an optional sprint name (blank = whole project). `sprint_metrics.py` requires a sprint name (exact match, case-insensitive) — it computes metrics for one sprint per run.

Requires a `.env` file (not committed) in the repo root with:

```
JIRA_BASE_URL=https://<your-domain>.atlassian.net
JIRA_EMAIL=<jira account email>
JIRA_API_TOKEN=<atlassian api token>
JIRA_PROJECT_KEY=<project key, e.g. GOPER>
JIRA_BOARD_ID=<optional — only needed if the project has 0 or 2+ scrum boards>
```

`sprint_metrics.py` auto-detects the project's scrum board (`type=scrum`) if `JIRA_BOARD_ID` is unset; it exits with an error listing candidates if that's ambiguous.

## Environment setup

A `.venv` already exists in the repo. To reinstall dependencies:

```bash
pip install -r requirements.txt
```

## Architecture

### `jira_client.py`

Shared helpers, imported by both `main.py` and `sprint_metrics.py`. Loads `.env`, builds HTTP Basic Auth, and exposes:

- `jira_get(path, params, max_retries)` — the sole HTTP wrapper around `requests`. Handles 429 rate limiting (respects `Retry-After`), retries on timeout/connection errors with linear backoff, and returns `None` on exhausted retries (callers must handle `None`). All Jira API calls go through this — don't call `requests` directly.
- `get_field_map()` — calls `/rest/api/3/field` and resolves custom field IDs by human-readable name, since `customfield_XXXXX` IDs differ per Jira instance. Never hardcode a `customfield_XXXXX` ID.
- `fetch_all_issues(jql, fields)` — paginates `/rest/api/3/search/jql` using `nextPageToken` (not `startAt`) at 100 results/page.
- `fetch_issue_changelog(issue_key)` — paginates `/rest/api/3/issue/{key}/changelog` per-issue using `startAt`/`isLast`. One call per issue — the main runtime cost of both scripts for large result sets.
- `normalize_value(value)` — flattens Jira's field shapes (dicts with `value`/`name`/`displayName`/`key`, or lists of same) into plain strings/values.
- `seconds_to_hours(value)` — converts Jira's second-based time-tracking fields to hours (rounded to 2 decimals).

### `main.py`

Flat top-to-bottom script (no `if __name__ == "__main__"` guard, by convention). Builds JQL from the sprint prompt, resolves custom field IDs via `get_field_map()`, fetches all matching issues + their changelogs, and writes `output/jira_export_<timestamp>.xlsx` with three sheets: `issues` (current state snapshot), `field_changelog` (every field change, full audit trail), `status_changelog` (subset of the above filtered to `field == "status"`). If a changelog fetch fails for an issue, a sentinel row with `"ERRO_AO_BUSCAR_CHANGELOG"` is inserted instead of silently dropping the issue.

### `sprint_metrics.py`

Wrapped in a `main()` (unlike `main.py`'s flat style — this script is complex enough to need testable functions). High-level flow:

1. Resolve the board (`resolve_board_id`) and the named sprint (`resolve_sprint`, via `/rest/agile/1.0/board/{id}/sprint`), getting `startDate`/`endDate`/`completeDate`.
2. Use the **Greenhopper internal API** (`/rest/greenhopper/1.0/rapid/charts/sprintreport` — undocumented, but the only practical way to get "which top-level issues passed through this sprint" without hand-reconstructing everything from changelog) to discover the universe of top-level issues (Story/Bug/Task/Epic) touched by the sprint.
3. Fetch all Subtarefas (`issuetype in (10003, 10110)` — **two different issue types share the display name "Subtarefa" in this Jira instance; JQL by name silently matches zero, always filter by these IDs**) whose `parent` is in that set.
4. Classify every issue (both the top-level issues and each Subtarefa, via `classify_by_sprint_field`) as `planejada` / `adicionada` / `removida` by reconstructing the `Sprint` custom field's value over time from its own changelog, and comparing a snapshot at a **planning cutoff** (`PLANNING_WINDOW_DAYS` = 5 days after the sprint's official start — not the literal start instant, because real data showed scope gets bulk-replanned a few days in, not drifted gradually) against a snapshot at the sprint's end. Subtasks with no `Sprint`-field changelog of their own (common — the field is often set silently at creation) fall back to their parent's classification.
5. For each Subtarefa, walk its status changelog to compute Lead Time, blocked time, review time, and rework count — see "Status stage detection" below.
6. Write `output/sprint_metrics_<sprint>_<timestamp>.xlsx`.

### `business_hours.py`

`business_hours_between(start, end)` — sums business hours (seg-sex, 9h-18h, Brazilian national holidays excluded via the `holidays` package) between two timestamps. Jira changelog timestamps come in `-03:00`; the Agile API's sprint `startDate`/`endDate` come in UTC (`Z`) — both get normalized to `America/Sao_Paulo` before comparing, otherwise the math is off by up to 3 hours.

## Status stage detection (important context, not obvious from the code)

The project **migrated workflows** around jun/2026: an old granular flow (`Backlog/A Fazer → DESENVOLVIMENTO/Em progresso → Em Revisao → QA → HOMOLOGAÇÃO → ANÁLISE → PRONTO P/ TESTES → PRONTO P/ PROD → Pronto`, plus `Bloqueado`/`CORREÇÃO`/`AGUARDANDO CORREÇÃO`/`Reprovado`) caused cards to sit for many sprints in intermediate statuses like `PRONTO P/ TESTES`, distorting completion analysis — so the team simplified to 6 statuses: `Backlog`, `Bloqueado`, `EM ANDAMENTO`, `Em Revisao`, `ANÁLISE`, `Pronto`. `sprint_metrics.py` has to work across both regimes at once (old sprints' history still contains the retired status names):

- **Start of progress / done** are detected via **`statusCategory`** (fetched live from `/rest/api/3/project/{key}/statuses`, not hardcoded), not literal status names — this alone is robust across both workflow regimes without a name list.
- **Rework** (3.6) is detected via literal transitions *out of* `"Em Revisao"` into an alias list (`REWORK_TARGET_STATUSES` in `sprint_metrics.py`): `CORREÇÃO`/`Reprovado` (old regime) and `ANÁLISE` (new regime — confirmed with the team; `ANÁLISE` is dual-purpose, used both as a pre-dev step and as the post-review rework destination, disambiguated by requiring the transition to originate from `Em Revisao`).
- `OLD_REGIME_STATUSES` in `sprint_metrics.py` flags a sprint's `regime_workflow` as `"antigo"` if any retired status appears in its cards' changelogs — Lead Time from old-regime sprints tends to be inflated by cards stuck in now-retired intermediate statuses, and isn't directly comparable to new-regime sprints.

If the workflow changes again, extend the alias sets/status lists above rather than adding literal-status branches elsewhere in the code.

## Conventions to preserve

- User-facing console output (progress messages, prompts) is in Portuguese (pt-BR).
- Never hardcode a `customfield_XXXXX` ID; resolve custom fields through `get_field_map()` by name.
- Never hardcode a Jira status name where a `statusCategory`-based check would work instead — this codebase has already been burned once by a workflow migration invalidating hardcoded status names.
- API calls always go through `jira_get` in `jira_client.py`.
