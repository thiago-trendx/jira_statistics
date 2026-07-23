import os
import time
from datetime import datetime

import pandas as pd

from jira_client import (
    PROJECT_KEY,
    fetch_all_issues,
    fetch_issue_changelog,
    get_field_map,
    normalize_value,
    seconds_to_hours,
)

SPRINT_NAME = input(
    "Digite o nome da sprint (Enter para buscar todo o projeto): "
).strip()

if SPRINT_NAME:
    JQL = (
        f'project = "{PROJECT_KEY}" '
        f'AND sprint = "{SPRINT_NAME}" '
        f'ORDER BY created DESC'
    )
else:
    JQL = (
        f'project = "{PROJECT_KEY}" '
        f'ORDER BY created DESC'
    )

print("JQL utilizada:")
print(JQL)

field_map = get_field_map()

STORY_POINTS_FIELD = (
    field_map.get("story points")
    or field_map.get("story point estimate")
)

SPRINT_FIELD = field_map.get("sprint")
MOTIVO_BLOQUEIO_FIELD = field_map.get("motivo bloqueio")
MOTIVO_REABERTURA_FIELD = field_map.get("motivo reabertura")

print("Campos detectados:")
print("Story Points:", STORY_POINTS_FIELD)
print("Sprint:", SPRINT_FIELD)
print("Motivo Bloqueio:", MOTIVO_BLOQUEIO_FIELD)
print("Motivo Reabertura:", MOTIVO_REABERTURA_FIELD)


base_fields = [
    "summary",
    "status",
    "issuetype",
    "parent",
    "created",
    "updated",
    "assignee",
    "reporter",
    "labels",
    "components",
    "fixVersions",
    "resolution",
    "resolutiondate",
    "timeoriginalestimate",
    "timeestimate",
    "timespent",
    "aggregatetimeoriginalestimate",
    "aggregatetimeestimate",
    "aggregatetimespent",
    "timetracking",
]

custom_fields = [
    STORY_POINTS_FIELD,
    SPRINT_FIELD,
    MOTIVO_BLOQUEIO_FIELD,
    MOTIVO_REABERTURA_FIELD,
]

ISSUE_FIELDS = ",".join(
    [field for field in base_fields + custom_fields if field]
)


issues = fetch_all_issues(JQL, ISSUE_FIELDS)
print("Issues encontradas:", len(issues))

issues_rows = []
status_changelog_rows = []
field_changelog_rows = []

for issue in issues:
    issue_key = issue["key"]
    fields = issue["fields"]

    parent = fields.get("parent") or {}
    status = fields.get("status") or {}
    status_category = status.get("statusCategory") or {}

    labels = fields.get("labels") or []
    components = fields.get("components") or []
    fix_versions = fields.get("fixVersions") or []

    story_points = normalize_value(fields.get(STORY_POINTS_FIELD)) if STORY_POINTS_FIELD else None
    sprint = normalize_value(fields.get(SPRINT_FIELD)) if SPRINT_FIELD else None
    motivo_bloqueio = normalize_value(fields.get(MOTIVO_BLOQUEIO_FIELD)) if MOTIVO_BLOQUEIO_FIELD else None
    motivo_reabertura = normalize_value(fields.get(MOTIVO_REABERTURA_FIELD)) if MOTIVO_REABERTURA_FIELD else None

    issues_rows.append({
        "issue_key": issue_key,
        "summary": fields.get("summary"),
        "issue_type": fields.get("issuetype", {}).get("name"),
        "parent_key": parent.get("key"),
        "parent_summary": parent.get("fields", {}).get("summary"),
        "current_status": status.get("name"),
        "status_category": status_category.get("name"),
        "assignee": normalize_value(fields.get("assignee")),
        "reporter": normalize_value(fields.get("reporter")),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "resolution": normalize_value(fields.get("resolution")),
        "resolutiondate": fields.get("resolutiondate"),

        "labels": ", ".join(labels),
        "components": normalize_value(components),
        "fix_versions": normalize_value(fix_versions),

        "sprint": sprint,
        "story_points": story_points,
        "motivo_bloqueio": motivo_bloqueio,
        "motivo_reabertura": motivo_reabertura,

        "original_estimate_seconds": fields.get("timeoriginalestimate"),
        "remaining_estimate_seconds": fields.get("timeestimate"),
        "time_spent_seconds": fields.get("timespent"),

        "original_estimate_hours": seconds_to_hours(fields.get("timeoriginalestimate")),
        "remaining_estimate_hours": seconds_to_hours(fields.get("timeestimate")),
        "time_spent_hours": seconds_to_hours(fields.get("timespent")),

        "aggregate_original_estimate_seconds": fields.get("aggregatetimeoriginalestimate"),
        "aggregate_remaining_estimate_seconds": fields.get("aggregatetimeestimate"),
        "aggregate_time_spent_seconds": fields.get("aggregatetimespent"),

        "aggregate_original_estimate_hours": seconds_to_hours(fields.get("aggregatetimeoriginalestimate")),
        "aggregate_remaining_estimate_hours": seconds_to_hours(fields.get("aggregatetimeestimate")),
        "aggregate_time_spent_hours": seconds_to_hours(fields.get("aggregatetimespent")),
    })

    histories = fetch_issue_changelog(issue_key)

    if histories is None:
        status_changelog_rows.append({
            "issue_key": issue_key,
            "summary": fields.get("summary"),
            "issue_type": fields.get("issuetype", {}).get("name"),
            "parent_key": parent.get("key"),
            "changed_at": None,
            "author": None,
            "from_status": "ERRO_AO_BUSCAR_CHANGELOG",
            "to_status": None,
        })

        field_changelog_rows.append({
            "issue_key": issue_key,
            "summary": fields.get("summary"),
            "issue_type": fields.get("issuetype", {}).get("name"),
            "parent_key": parent.get("key"),
            "changed_at": None,
            "author": None,
            "field": "ERRO_AO_BUSCAR_CHANGELOG",
            "from_value": None,
            "to_value": None,
        })

        continue

    for history in histories:
        changed_at = history.get("created")
        author = history.get("author", {}).get("displayName")

        for item in history.get("items", []):
            field_name = item.get("field")
            from_value = item.get("fromString")
            to_value = item.get("toString")

            field_changelog_rows.append({
                "issue_key": issue_key,
                "summary": fields.get("summary"),
                "issue_type": fields.get("issuetype", {}).get("name"),
                "parent_key": parent.get("key"),
                "changed_at": changed_at,
                "author": author,
                "field": field_name,
                "from_value": from_value,
                "to_value": to_value,
            })

            if field_name == "status":
                status_changelog_rows.append({
                    "issue_key": issue_key,
                    "summary": fields.get("summary"),
                    "issue_type": fields.get("issuetype", {}).get("name"),
                    "parent_key": parent.get("key"),
                    "changed_at": changed_at,
                    "author": author,
                    "from_status": from_value,
                    "to_status": to_value,
                })

    time.sleep(0.2)

print("Transições de status encontradas:", len(status_changelog_rows))
print("Alterações de campos encontradas:", len(field_changelog_rows))

os.makedirs("output", exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
output_path = f"output/jira_export_{timestamp}.xlsx"

with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
    pd.DataFrame(issues_rows).to_excel(writer, sheet_name="issues", index=False)
    pd.DataFrame(status_changelog_rows).to_excel(writer, sheet_name="status_changelog", index=False)
    pd.DataFrame(field_changelog_rows).to_excel(writer, sheet_name="field_changelog", index=False)

print(f"Excel gerado em: {output_path}")