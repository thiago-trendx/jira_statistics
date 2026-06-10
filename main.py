import os
from datetime import datetime
import requests
import pandas as pd
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth
import time
import requests

load_dotenv()

BASE_URL = os.getenv("JIRA_BASE_URL", "").strip()
EMAIL = os.getenv("JIRA_EMAIL", "").strip()
TOKEN = os.getenv("JIRA_API_TOKEN", "").strip()
PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "").strip()

auth = HTTPBasicAuth(EMAIL, TOKEN)
headers = {"Accept": "application/json"}

JQL = f'project = "{PROJECT_KEY}" AND sprint = "Magneto" ORDER BY created DESC'

def jira_get(path, params=None, max_retries=3):
    url = f"{BASE_URL}{path}"

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(
                url,
                headers=headers,
                auth=auth,
                params=params,
                timeout=30,
            )

            print("GET", response.url, "->", response.status_code)

            if response.status_code == 429:
                wait_seconds = int(response.headers.get("Retry-After", "10"))
                print(f"Rate limit. Aguardando {wait_seconds}s...")
                time.sleep(wait_seconds)
                continue

            response.raise_for_status()
            return response.json()

        except requests.exceptions.ReadTimeout:
            print(f"Timeout na tentativa {attempt}/{max_retries}: {url}")
            time.sleep(5 * attempt)

        except requests.exceptions.ConnectionError as e:
            print(f"Erro de conexão na tentativa {attempt}/{max_retries}: {e}")
            time.sleep(5 * attempt)

    print(f"Falha após {max_retries} tentativas: {url}")
    return None

# 1) Buscar issues da sprint
search_data = jira_get(
    "/rest/api/3/search/jql",
    params={
        "jql": JQL,
        "maxResults": 100,
        "fields": "summary,status,issuetype,parent,created,updated,assignee",
    },
)

issues = search_data.get("issues", [])
print("Issues encontradas:", len(issues))

issues_rows = []
changelog_rows = []

for issue in issues:
    issue_key = issue["key"]
    fields = issue["fields"]

    issues_rows.append({
        "issue_key": issue_key,
        "summary": fields.get("summary"),
        "issue_type": fields.get("issuetype", {}).get("name"),
        "parent_key": fields.get("parent", {}).get("key") if fields.get("parent") else None,
        "current_status": fields.get("status", {}).get("name"),
        "assignee": fields.get("assignee", {}).get("displayName") if fields.get("assignee") else None,
        "created": fields.get("created"),
        "updated": fields.get("updated"),
    })

    # 2) Buscar changelog da issue individualmente
    start_at = 0

    while True:
        changelog_data = jira_get(
            f"/rest/api/3/issue/{issue_key}/changelog",
            params={
                "maxResults": 100,
                "startAt": start_at,
            },
        )

        if changelog_data is None:
            changelog_rows.append({
                "issue_key": issue_key,
                "summary": fields.get("summary"),
                "issue_type": fields.get("issuetype", {}).get("name"),
                "parent_key": fields.get("parent", {}).get("key") if fields.get("parent") else None,
                "changed_at": None,
                "author": None,
                "from_status": "ERRO_AO_BUSCAR_CHANGELOG",
                "to_status": None,
            })
            break

        histories = changelog_data.get("values", [])

        for history in histories:
            changed_at = history.get("created")
            author = history.get("author", {}).get("displayName")

            for item in history.get("items", []):
                if item.get("field") == "status":
                    changelog_rows.append({
                        "issue_key": issue_key,
                        "summary": fields.get("summary"),
                        "issue_type": fields.get("issuetype", {}).get("name"),
                        "parent_key": fields.get("parent", {}).get("key") if fields.get("parent") else None,
                        "changed_at": changed_at,
                        "author": author,
                        "from_status": item.get("fromString"),
                        "to_status": item.get("toString"),
                    })

        if changelog_data.get("isLast", True):
            break

        start_at += changelog_data.get("maxResults", 100)

    time.sleep(0.2)

print("Transições de status encontradas:", len(changelog_rows))

os.makedirs("output", exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
output_path = f"output/jira_export_{timestamp}.xlsx"

with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
    pd.DataFrame(issues_rows).to_excel(writer, sheet_name="issues", index=False)
    pd.DataFrame(changelog_rows).to_excel(writer, sheet_name="status_changelog", index=False)

print(f"Excel gerado em: {output_path}")