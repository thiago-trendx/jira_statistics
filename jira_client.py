import os
import time

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

load_dotenv()

BASE_URL = os.getenv("JIRA_BASE_URL", "").strip()
EMAIL = os.getenv("JIRA_EMAIL", "").strip()
TOKEN = os.getenv("JIRA_API_TOKEN", "").strip()
PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "").strip()
BOARD_ID = os.getenv("JIRA_BOARD_ID", "").strip()

auth = HTTPBasicAuth(EMAIL, TOKEN)
headers = {"Accept": "application/json"}


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


def seconds_to_hours(value):
    if value is None:
        return None
    return round(value / 3600, 2)


def normalize_value(value):
    if value is None:
        return None

    if isinstance(value, dict):
        if "value" in value:
            return value["value"]
        if "name" in value:
            return value["name"]
        if "displayName" in value:
            return value["displayName"]
        if "key" in value:
            return value["key"]
        return str(value)

    if isinstance(value, list):
        return ", ".join([str(normalize_value(item)) for item in value])

    return value


def get_field_map():
    data = jira_get("/rest/api/3/field")
    field_map = {}

    for field in data or []:
        name = field.get("name")
        field_id = field.get("id")
        if name and field_id:
            field_map[name.lower()] = field_id

    return field_map


def fetch_all_issues(jql, fields):
    all_issues = []
    next_page_token = None
    page = 1

    while True:
        params = {
            "jql": jql,
            "maxResults": 100,
            "fields": fields,
        }

        if next_page_token:
            params["nextPageToken"] = next_page_token

        search_data = jira_get(
            "/rest/api/3/search/jql",
            params=params,
        )

        if search_data is None:
            print("Erro ao buscar issues.")
            break

        issues_page = search_data.get("issues", [])
        all_issues.extend(issues_page)

        print(f"Página {page}: {len(issues_page)} issues")
        print(f"Total acumulado: {len(all_issues)} issues")

        if search_data.get("isLast", True):
            break

        next_page_token = search_data.get("nextPageToken")

        if not next_page_token:
            print("Busca interrompida: API informou que há mais páginas, mas não enviou nextPageToken.")
            break

        page += 1
        time.sleep(0.2)

    return all_issues


def fetch_issue_changelog(issue_key):
    all_histories = []
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
            return None

        histories = changelog_data.get("values", [])
        all_histories.extend(histories)

        if changelog_data.get("isLast", True):
            break

        start_at += changelog_data.get("maxResults", 100)
        time.sleep(0.1)

    return all_histories
