import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
from dateutil import parser as dtparser

from business_hours import WORKDAY_END, WORKDAY_START, business_hours_between
from jira_client import (
    BOARD_ID,
    PROJECT_KEY,
    fetch_all_issues,
    fetch_issue_changelog,
    jira_get,
)

SUBTASK_TYPE_IDS = ["10003", "10110"]

# Horas úteis por dia (seg-sex, 9h-18h = 9h), para converter tempo bloqueado
# de horas para dias úteis nos números da apresentação.
BUSINESS_HOURS_PER_DAY = (
    WORKDAY_END.hour + WORKDAY_END.minute / 60
) - (WORKDAY_START.hour + WORKDAY_START.minute / 60)

# "Planejado no início" não usa o instante exato de sprint.startDate: os dados
# mostram replanejamento em lote alguns dias após o clique em "Iniciar Sprint"
# (ex.: 36 issues adicionadas no mesmo instante, ~4,3 dias depois do início na
# sprint Copa), não deriva gradual de escopo. Uma issue conta como "planejada"
# se já estava na sprint até este número de dias corridos após o início oficial.
PLANNING_WINDOW_DAYS = 5

DONE_CATEGORY = "Itens concluídos"
TODO_CATEGORY = "Itens Pendentes"

# Visão ajustada para a apresentação: a equipe ainda está migrando para o
# workflow novo, e boa parte do trabalho fica "pronto" na prática antes de
# passar por QA/homologação/deploy. Qualquer status posterior à finalização
# do dev conta como concluído, além da statusCategory "Itens concluídos"
# (que hoje só cobre "Pronto"): QA, HOMOLOGAÇÃO, PRONTO P/ TESTES e
# PRONTO P/ PROD são todos pós-dev no fluxo antigo (o card entra em QA
# assim que o dev termina sua parte, mesmo que homologação/deploy demorem).
EFFECTIVE_DONE_STATUSES = {"QA", "HOMOLOGAÇÃO", "PRONTO P/ TESTES", "PRONTO P/ PROD"}

REVIEW_STATUS = "Em Revisao"
BLOCKED_STATUS = "Bloqueado"
REWORK_TARGET_STATUSES = {"CORREÇÃO", "Reprovado", "ANÁLISE"}

# Status usados só no fluxo granular antigo (aposentado por volta de jun/2026).
# Se algum aparecer no changelog da sprint, marcamos a sprint como regime "antigo".
OLD_REGIME_STATUSES = {
    "QA", "HOMOLOGAÇÃO", "PRONTO P/ TESTES", "PRONTO P/ PROD",
    "CORREÇÃO", "AGUARDANDO CORREÇÃO", "Reprovado", "DESENVOLVIMENTO",
    "Em progresso", "A Fazer", "Reopened",
}


# ---------------------------------------------------------------------------
# Descoberta de board / sprint
# ---------------------------------------------------------------------------

def resolve_board_id():
    if BOARD_ID:
        return int(BOARD_ID)

    data = jira_get(
        "/rest/agile/1.0/board",
        params={"projectKeyOrId": PROJECT_KEY, "type": "scrum"},
    )
    boards = (data or {}).get("values", [])

    if len(boards) != 1:
        print(f"Encontrados {len(boards)} boards scrum para o projeto {PROJECT_KEY}.")
        for b in boards:
            print(" -", b.get("id"), b.get("name"))
        print("Defina JIRA_BOARD_ID no .env para escolher qual usar.")
        sys.exit(1)

    return boards[0]["id"]


def resolve_sprint(board_id, sprint_name):
    target = sprint_name.strip().lower()
    start_at = 0

    while True:
        data = jira_get(
            f"/rest/agile/1.0/board/{board_id}/sprint",
            params={"state": "active,closed", "startAt": start_at, "maxResults": 50},
        )
        if data is None:
            return None

        for sprint in data.get("values", []):
            if sprint.get("name", "").strip().lower() == target:
                return sprint

        if data.get("isLast", True):
            return None

        start_at += data.get("maxResults", 50)


# ---------------------------------------------------------------------------
# Escopo da sprint (Greenhopper para nível superior, changelog para Subtarefa)
# ---------------------------------------------------------------------------

def fetch_sprint_report_contents(board_id, sprint_id):
    data = jira_get(
        "/rest/greenhopper/1.0/rapid/charts/sprintreport",
        params={"rapidViewId": board_id, "sprintId": sprint_id},
    )
    return (data or {}).get("contents", {})


def all_parent_keys(contents):
    keys = set()
    for bucket in (
        "completedIssues",
        "issuesNotCompletedInCurrentSprint",
        "puntedIssues",
        "issuesCompletedInAnotherSprint",
    ):
        for item in contents.get(bucket, []):
            key = item.get("key")
            if key:
                keys.add(key)
    return keys


def fetch_subtasks_for_parents(parent_keys):
    if not parent_keys:
        return []

    keys_list = ",".join(f'"{k}"' for k in sorted(parent_keys))
    jql = (
        f'project = "{PROJECT_KEY}" '
        f'AND issuetype in ({",".join(SUBTASK_TYPE_IDS)}) '
        f'AND parent in ({keys_list})'
    )
    fields = "status,parent,created,timeoriginalestimate,timespent"
    return fetch_all_issues(jql, fields)


# ---------------------------------------------------------------------------
# Reconstrução do campo Sprint e classificação por Subtarefa
# ---------------------------------------------------------------------------

def extract_transitions(histories):
    """Retorna (status_transitions, sprint_changes), cada um ordenado por data.
    status_transitions: lista de (changed_at, from_status, to_status)
    sprint_changes: lista de (changed_at, to_value) do campo Sprint"""
    status_transitions = []
    sprint_changes = []

    for history in histories or []:
        changed_at = dtparser.isoparse(history["created"])
        for item in history.get("items", []):
            field_name = item.get("field")
            if field_name == "status":
                status_transitions.append((changed_at, item.get("fromString"), item.get("toString")))
            elif field_name == "Sprint":
                sprint_changes.append((changed_at, item.get("toString")))

    status_transitions.sort(key=lambda t: t[0])
    sprint_changes.sort(key=lambda t: t[0])
    return status_transitions, sprint_changes


def sprint_snapshot_at(sprint_changes, target_dt):
    """Nomes de sprint presentes no campo Sprint no instante target_dt.
    O changelog guarda em to_value a lista completa após cada mudança
    (não um delta), então basta pegar o valor mais recente <= target_dt."""
    latest_value = None
    for changed_at, to_value in sprint_changes:
        if changed_at <= target_dt:
            latest_value = to_value
        else:
            break

    if not latest_value:
        return set()
    return {name.strip() for name in latest_value.split(",") if name.strip()}


def classify_by_creation_date(created_dt, planning_cutoff_dt):
    """Fallback quando a Subtarefa não tem changelog próprio do campo Sprint
    (comum — o campo é herdado do pai só na criação, sem gerar evento de
    mudança). Usa só a data de criação da própria Subtarefa — não a
    classificação da História-mãe, que pode estar presa há meses rolando de
    sprint em sprint e contaminaria a leitura."""
    return "planejada" if created_dt <= planning_cutoff_dt else "adicionada"


def classify_by_sprint_field(sprint_changes, sprint_name, planning_cutoff_dt, end_dt, fallback_label):
    """Classifica uma Subtarefa como 'planejada', 'adicionada' ou 'removida'
    com base no próprio changelog do campo Sprint. Usa fallback_label (ver
    classify_by_creation_date) quando não há changelog próprio de Sprint."""
    if not sprint_changes:
        return fallback_label

    in_start = sprint_name in sprint_snapshot_at(sprint_changes, planning_cutoff_dt)
    in_end = sprint_name in sprint_snapshot_at(sprint_changes, end_dt)

    if in_start and in_end:
        return "planejada"
    if in_start and not in_end:
        return "removida"
    if not in_start and in_end:
        return "adicionada"
    return fallback_label


# ---------------------------------------------------------------------------
# Métricas de fluxo por card (lead time, bloqueio, revisão, retrabalho)
# ---------------------------------------------------------------------------

def status_at(status_transitions, target_dt, current_status_fallback):
    if not status_transitions:
        return current_status_fallback

    if target_dt < status_transitions[0][0]:
        return status_transitions[0][1] or current_status_fallback

    latest = status_transitions[0][1]
    for changed_at, _from_status, to_status in status_transitions:
        if changed_at <= target_dt:
            latest = to_status
        else:
            break
    return latest


def is_effectively_done(status_name, status_category_map):
    return status_category_map.get(status_name) == DONE_CATEGORY or status_name in EFFECTIVE_DONE_STATUSES


def compute_lead_time_hours(status_transitions, status_category_map):
    start_ts = None
    for changed_at, _from_status, to_status in status_transitions:
        to_category = status_category_map.get(to_status)
        if start_ts is None and to_category and to_category != TODO_CATEGORY:
            start_ts = changed_at
        if is_effectively_done(to_status, status_category_map):
            if start_ts is None or changed_at <= start_ts:
                return None
            return business_hours_between(start_ts, changed_at)
    return None


def _sum_status_intervals(status_transitions, target_status, reference_end_dt):
    """Soma horas úteis em todos os intervalos em que o status foi target_status,
    fechando um intervalo ainda aberto em reference_end_dt."""
    total = 0.0
    entered_at = None

    for changed_at, from_status, to_status in status_transitions:
        if to_status == target_status and entered_at is None:
            entered_at = changed_at
        elif from_status == target_status and entered_at is not None:
            total += business_hours_between(entered_at, changed_at)
            entered_at = None

    if entered_at is not None:
        total += business_hours_between(entered_at, reference_end_dt)

    return round(total, 2)


def compute_blocked_hours(status_transitions, reference_end_dt):
    return _sum_status_intervals(status_transitions, BLOCKED_STATUS, reference_end_dt)


def count_blocked_entries(status_transitions):
    return sum(1 for _c, _f, to_status in status_transitions if to_status == BLOCKED_STATUS)


def count_review_cycles_and_rework(status_transitions):
    review_entries = 0
    rework_count = 0
    for _changed_at, from_status, to_status in status_transitions:
        if to_status == REVIEW_STATUS:
            review_entries += 1
        if from_status == REVIEW_STATUS and to_status in REWORK_TARGET_STATUSES:
            rework_count += 1
    return review_entries, rework_count


def sprint_uses_old_regime(all_status_transitions):
    for transitions in all_status_transitions:
        for _changed_at, from_status, to_status in transitions:
            if from_status in OLD_REGIME_STATUSES or to_status in OLD_REGIME_STATUSES:
                return True
    return False


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------

def main():
    sprint_name = input("Digite o nome da sprint: ").strip()
    if not sprint_name:
        print("É necessário informar o nome de uma sprint.")
        sys.exit(1)

    board_id = resolve_board_id()
    print("Board:", board_id)

    sprint = resolve_sprint(board_id, sprint_name)
    if sprint is None:
        print(f"Sprint '{sprint_name}' não encontrada no board {board_id}.")
        sys.exit(1)

    start_dt = dtparser.isoparse(sprint["startDate"])
    planning_cutoff_dt = start_dt + timedelta(days=PLANNING_WINDOW_DAYS)
    complete_dt = sprint.get("completeDate")
    end_dt = dtparser.isoparse(complete_dt) if complete_dt else dtparser.isoparse(sprint["endDate"])

    if sprint.get("state") == "active":
        print("Aviso: a sprint ainda está ativa. As métricas são um snapshot parcial até agora.")
        end_dt = datetime.now().astimezone()

    print(f"Sprint '{sprint['name']}' (id={sprint['id']}): {start_dt} até {end_dt}")
    print(f"Janela de planejamento (planejado = presente até): {planning_cutoff_dt}")

    contents = fetch_sprint_report_contents(board_id, sprint["id"])
    parent_keys = all_parent_keys(contents)
    print(f"Issues de nível superior que passaram pela sprint: {len(parent_keys)}")

    subtasks = fetch_subtasks_for_parents(parent_keys)
    print(f"Subtarefas encontradas: {len(subtasks)}")

    status_category_map = {}
    for issuetype_statuses in jira_get(f"/rest/api/3/project/{PROJECT_KEY}/statuses") or []:
        for s in issuetype_statuses.get("statuses", []):
            status_category_map[s.get("name")] = (s.get("statusCategory") or {}).get("name")

    cards = []
    all_status_transitions = []

    for subtask in subtasks:
        key = subtask["key"]
        fields = subtask["fields"]
        parent_key = (fields.get("parent") or {}).get("key")
        current_status_name = (fields.get("status") or {}).get("name")
        created_dt = dtparser.isoparse(fields["created"])

        histories = fetch_issue_changelog(key)
        status_transitions, sprint_changes = extract_transitions(histories)
        all_status_transitions.append(status_transitions)

        fallback_label = classify_by_creation_date(created_dt, planning_cutoff_dt)
        label = classify_by_sprint_field(
            sprint_changes, sprint["name"], planning_cutoff_dt, end_dt, fallback_label=fallback_label,
        )

        status_no_fim = status_at(status_transitions, end_dt, current_status_name)
        entregue = is_effectively_done(status_no_fim, status_category_map)
        bloqueado_no_fim = status_no_fim == BLOCKED_STATUS

        review_entries, rework_count = count_review_cycles_and_rework(status_transitions)

        cards.append({
            "issue_key": key,
            "parent_key": parent_key,
            "classificacao": label,
            "status_no_fim_sprint": status_no_fim,
            "entregue": entregue,
            "bloqueado_no_fim": bloqueado_no_fim,
            "lead_time_horas": compute_lead_time_hours(status_transitions, status_category_map),
            "tempo_bloqueado_horas": compute_blocked_hours(status_transitions, end_dt),
            "vezes_bloqueado": count_blocked_entries(status_transitions),
            "ciclos_revisao": review_entries,
            "retrabalhos": rework_count,
            "estimativa_original_segundos": fields.get("timeoriginalestimate"),
            "tempo_gasto_segundos": fields.get("timespent"),
        })

        time.sleep(0.2)

    df = pd.DataFrame(cards)
    regime = "antigo" if sprint_uses_old_regime(all_status_transitions) else "novo"

    planejadas = df[df["classificacao"] == "planejada"]
    adicionadas = df[df["classificacao"] == "adicionada"]
    removidas = df[df["classificacao"] == "removida"]
    em_escopo_final = df[df["classificacao"].isin(["planejada", "adicionada"])]

    n_planejadas = len(planejadas)
    n_adicionadas = len(adicionadas)
    n_em_escopo_final = len(em_escopo_final)

    pct_conclusao_total = (
        em_escopo_final["entregue"].sum() / n_em_escopo_final if n_em_escopo_final else None
    )
    pct_conclusao_planejado = (
        planejadas["entregue"].sum() / n_planejadas if n_planejadas else None
    )
    desvio_escopo = n_adicionadas / n_planejadas if n_planejadas else None
    pct_bloqueados_final = (
        em_escopo_final["bloqueado_no_fim"].sum() / n_planejadas if n_planejadas else None
    )
    tempo_bloqueado_total = em_escopo_final["tempo_bloqueado_horas"].sum()

    cards_bloqueados = em_escopo_final[em_escopo_final["vezes_bloqueado"] > 0]
    n_cards_bloqueados = len(cards_bloqueados)
    tempo_medio_bloqueio_dias = (
        (cards_bloqueados["tempo_bloqueado_horas"].mean() / BUSINESS_HOURS_PER_DAY)
        if n_cards_bloqueados else None
    )

    n_entregues = int(em_escopo_final["entregue"].sum())

    lead_times = em_escopo_final["lead_time_horas"].dropna()
    lead_time_medio = lead_times.mean() if not lead_times.empty else None
    lead_time_mediano = lead_times.median() if not lead_times.empty else None

    total_ciclos_revisao = em_escopo_final["ciclos_revisao"].sum()
    total_retrabalhos = em_escopo_final["retrabalhos"].sum()
    taxa_retrabalho = total_retrabalhos / total_ciclos_revisao if total_ciclos_revisao else None

    precisao_base = em_escopo_final[
        em_escopo_final["estimativa_original_segundos"].notna()
        & em_escopo_final["tempo_gasto_segundos"].notna()
    ]
    if not precisao_base.empty:
        total_estimado = precisao_base["estimativa_original_segundos"].sum()
        total_gasto = precisao_base["tempo_gasto_segundos"].sum()
        precisao_estimativa = total_gasto / total_estimado if total_estimado else None
    else:
        precisao_estimativa = None

    DONE_LABEL = "'Pronto', QA, HOMOLOGAÇÃO, PRONTO P/ TESTES ou PRONTO P/ PROD"

    resumo_rows = [
        {"metrica": "3.1.1", "nome": "Conclusão do escopo total", "valor": pct_conclusao_total,
         "formula": f"entregues / (planejado_início + adicionado); 'entregue' inclui {DONE_LABEL}",
         "base_n": n_em_escopo_final},
        {"metrica": "3.1.2", "nome": "Conclusão do escopo planejado", "valor": pct_conclusao_planejado,
         "formula": f"entregues_dos_planejados / planejado_início; 'entregue' inclui {DONE_LABEL}",
         "base_n": n_planejadas},
        {"metrica": "3.2.1", "nome": "Desvio de escopo", "valor": desvio_escopo,
         "formula": "adicionado / planejado_início", "base_n": n_planejadas},
        {"metrica": "3.2.2", "nome": "Precisão da estimativa", "valor": precisao_estimativa,
         "formula": "tempo_executado / tempo_estimado (só cards com os dois campos)", "base_n": len(precisao_base)},
        {"metrica": "3.3.1", "nome": "% cards bloqueados ao final", "valor": pct_bloqueados_final,
         "formula": "bloqueados_no_fim / planejado_início", "base_n": n_planejadas},
        {"metrica": "3.3.2", "nome": "Tempo total bloqueado (h úteis)", "valor": tempo_bloqueado_total,
         "formula": "soma dos intervalos em Bloqueado (seg-sex 9h-18h, sem feriados BR)", "base_n": n_em_escopo_final},
        {"metrica": "3.4", "nome": "Lead time médio (h úteis)", "valor": lead_time_medio,
         "formula": f"1ª saída de 'Itens Pendentes' até 1º status entre {DONE_LABEL}", "base_n": len(lead_times)},
        {"metrica": "3.4", "nome": "Lead time mediano (h úteis)", "valor": lead_time_mediano,
         "formula": f"1ª saída de 'Itens Pendentes' até 1º status entre {DONE_LABEL}", "base_n": len(lead_times)},
        {"metrica": "3.6", "nome": "Taxa de retrabalho", "valor": taxa_retrabalho,
         "formula": "transições Em Revisao→{CORREÇÃO,Reprovado,ANÁLISE} / entradas em Em Revisao",
         "base_n": total_ciclos_revisao},
        {"metrica": "extra", "nome": "Cards que passaram por Bloqueado (quantidade)", "valor": n_cards_bloqueados,
         "formula": "contagem de cards com pelo menos uma entrada em 'Bloqueado'", "base_n": n_em_escopo_final},
        {"metrica": "extra", "nome": "Tempo médio de bloqueio (dias úteis)", "valor": tempo_medio_bloqueio_dias,
         "formula": f"média de tempo_bloqueado_horas entre os cards bloqueados, ÷{BUSINESS_HOURS_PER_DAY:.0f}h/dia útil",
         "base_n": n_cards_bloqueados},
        {"metrica": "extra", "nome": "Cards concluídos (quantidade)", "valor": n_entregues,
         "formula": f"contagem de cards com status final entre {DONE_LABEL}", "base_n": n_em_escopo_final},
        {"metrica": "meta", "nome": "Regime de workflow", "valor": regime,
         "formula": "'antigo' se algum status do fluxo granular aparece no changelog da sprint", "base_n": len(df)},
        {"metrica": "meta", "nome": "Nota metodológica", "valor": "visão ajustada para apresentação",
         "formula": "3.5 (tempo em revisão) removida — será obtida via GitHub. "
                     f"'Entregue' considera qualquer status pós-dev como concluído ({DONE_LABEL}), "
                     "não só 'Pronto'. "
                     "Fallback de escopo usa a data de criação da própria Subtarefa, não a História-mãe.",
         "base_n": None},
    ]

    output_path = f"output/sprint_metrics_{sprint['name'].replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    os.makedirs("output", exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        pd.DataFrame(resumo_rows).to_excel(writer, sheet_name="resumo", index=False)
        planejadas.to_excel(writer, sheet_name="cards_planejados", index=False)
        adicionadas.to_excel(writer, sheet_name="cards_adicionados", index=False)
        removidas.to_excel(writer, sheet_name="cards_removidos", index=False)
        em_escopo_final.to_excel(writer, sheet_name="fluxo_detalhe", index=False)

    print(f"Excel gerado em: {output_path}")


if __name__ == "__main__":
    main()
