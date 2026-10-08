"""Simulação: inclui no escopo de uma sprint os cards que hoje não pertencem a nenhuma sprint
no Jira mas foram movidos para um status de conclusão (ou "Em Revisao") durante a sprint.

Motivação: parte do trabalho não é reportada na sprint (campo Sprint vazio ou card retirado depois),
então as métricas de sprint_metrics.py só enxergam o que foi registrado. Esta simulação mostra quanto
as métricas mudariam se esses cards estivessem no escopo.

Cenários calculados por sprint:
  A  só o escopo registrado (Subtarefas das issues da sprint, igual a sprint_metrics.py)
  B  A + Subtarefas fora de sprint
  C  B + Bug/Tarefa/História fora de sprint que não têm Subtarefas

Uso:
  python simulacao_fora_sprint.py "<sprint>" [--desde AAAA-MM-DD]
  (sem argumento, pergunta o nome da sprint)

--desde: data de referência para atribuir cada card a uma única sprint. Um card entra na sprint cujo
período contém o primeiro movimento dele (a partir de --desde) para um status concluído (ou, se nunca
concluiu, para "Em Revisao"). Para comparar sprints consecutivas sem contar o mesmo card duas vezes,
use a mesma data (a abertura da sprint mais antiga do grupo) em todas as execuções. Padrão: abertura
da própria sprint.

Variável de ambiente opcional EXTRA_DONE_STATUSES (ver sprint_metrics.py), ex.: "Em Revisao".

Limites: só entram os cards de fora que progrediram, então a conclusão é um teto e o desvio é um piso.
"Sem sprint" inclui cards nunca adicionados e cards retirados depois.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

import pandas as pd
from dateutil import parser as dtparser

import sprint_metrics as sm
from jira_client import PROJECT_KEY, fetch_all_issues, fetch_issue_changelog, jira_get

SUBTASK_TYPE = "Subtarefa"
EPIC_TYPE = "Epic"


def chunks(items, size=80):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch_by_keys(keys, fields):
    result = {}
    for part in chunks(keys):
        for issue in fetch_all_issues("key in (" + ",".join(part) + ")", fields):
            result[issue["key"]] = issue["fields"]
    return result


def first_anchor(status_transitions, desde_dt, done_statuses):
    """Instante do primeiro movimento, a partir de desde_dt, para um status concluído; se não houve,
    para REVIEW_STATUS. None se não houve nenhum dos dois."""
    done = [c for c, _f, to in status_transitions if c >= desde_dt and to in done_statuses]
    if done:
        return done[0]
    review = [c for c, _f, to in status_transitions if c >= desde_dt and to == sm.REVIEW_STATUS]
    return review[0] if review else None


def build_card(key, origem, fields, sprint_name, cutoff_dt, end_dt, status_category_map):
    transitions, sprint_changes = sm.extract_transitions(fetch_issue_changelog(key))
    created_dt = dtparser.isoparse(fields["created"])
    label = sm.classify_by_sprint_field(
        sprint_changes, sprint_name, cutoff_dt, end_dt,
        fallback_label=sm.classify_by_creation_date(created_dt, cutoff_dt),
    )
    status_no_fim = sm.status_at(transitions, end_dt, (fields.get("status") or {}).get("name"))
    review_entries, rework_count = sm.count_review_cycles_and_rework(transitions)
    return {
        "issue_key": key,
        "origem": origem,
        "tipo": (fields.get("issuetype") or {}).get("name"),
        "classificacao": label,
        "status_no_fim_sprint": status_no_fim,
        "entregue": sm.is_effectively_done(status_no_fim, status_category_map),
        "bloqueado_no_fim": status_no_fim == sm.BLOCKED_STATUS,
        "lead_time_horas": sm.compute_lead_time_hours(transitions, status_category_map),
        "tempo_bloqueado_horas": sm.compute_blocked_hours(transitions, end_dt),
        "vezes_bloqueado": sm.count_blocked_entries(transitions),
        "ciclos_revisao": review_entries,
        "retrabalhos": rework_count,
    }


def scenario_metrics(df):
    escopo = df[df["classificacao"].isin(["planejada", "adicionada"])]
    planejadas = escopo[escopo["classificacao"] == "planejada"]
    adicionadas = escopo[escopo["classificacao"] == "adicionada"]
    bloqueados = escopo[escopo["vezes_bloqueado"] > 0]
    lead = escopo["lead_time_horas"].dropna()
    ciclos = escopo["ciclos_revisao"].sum()
    sem_backlog = escopo[escopo["status_no_fim_sprint"] != "Backlog"]
    n_plan = len(planejadas)
    return {
        "cards_no_escopo": len(escopo),
        "planejadas": n_plan,
        "adicionadas": len(adicionadas),
        "concluidos": int(escopo["entregue"].sum()),
        "conclusao_total": escopo["entregue"].mean() if len(escopo) else None,
        "desvio_escopo": len(adicionadas) / n_plan if n_plan else None,
        "em_backlog": int((escopo["status_no_fim_sprint"] == "Backlog").sum()),
        "conclusao_sem_backlog": sem_backlog["entregue"].mean() if len(sem_backlog) else None,
        "bloqueados_ao_final_pct": escopo["bloqueado_no_fim"].sum() / n_plan if n_plan else None,
        "cards_que_passaram_por_bloqueio": len(bloqueados),
        "tempo_medio_bloqueio_dias_uteis": (
            bloqueados["tempo_bloqueado_horas"].mean() / sm.BUSINESS_HOURS_PER_DAY if len(bloqueados) else None
        ),
        "lead_time_mediano_h": lead.median() if len(lead) else None,
        "lead_time_medio_h": lead.mean() if len(lead) else None,
        "taxa_retrabalho": escopo["retrabalhos"].sum() / ciclos if ciclos else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sprint", nargs="?", help="nome da sprint (exato, sem diferenciar maiúsculas)")
    parser.add_argument("--desde", help="data de referência AAAA-MM-DD (padrão: abertura da sprint)")
    args = parser.parse_args()

    sprint_name = (args.sprint or input("Digite o nome da sprint: ")).strip()
    if not sprint_name:
        print("É necessário informar o nome de uma sprint.")
        sys.exit(1)

    board_id = sm.resolve_board_id()
    sprint = sm.resolve_sprint(board_id, sprint_name)
    if sprint is None:
        print(f"Sprint '{sprint_name}' não encontrada no board {board_id}.")
        sys.exit(1)

    start_dt = dtparser.isoparse(sprint["startDate"])
    cutoff_dt = start_dt + timedelta(days=sm.PLANNING_WINDOW_DAYS)
    complete = sprint.get("completeDate")
    end_dt = dtparser.isoparse(complete) if complete else dtparser.isoparse(sprint["endDate"])
    if sprint.get("state") == "active":
        print("Aviso: a sprint ainda está ativa. As métricas são um snapshot parcial até agora.")
        end_dt = datetime.now().astimezone()
    desde_dt = (
        dtparser.parse(args.desde).replace(tzinfo=start_dt.tzinfo) if args.desde else start_dt
    )
    print(f"Sprint '{sprint['name']}': {start_dt} até {end_dt} | referência (--desde): {desde_dt}")

    status_category_map = {}
    for issuetype_statuses in jira_get(f"/rest/api/3/project/{PROJECT_KEY}/statuses") or []:
        for s in issuetype_statuses.get("statuses", []):
            status_category_map[s.get("name")] = (s.get("statusCategory") or {}).get("name")
    done_statuses = {n for n, c in status_category_map.items() if c == sm.DONE_CATEGORY} | sm.EFFECTIVE_DONE_STATUSES
    movement_statuses = sorted(done_statuses | {sm.REVIEW_STATUS})

    # Escopo registrado: Subtarefas das issues de nível superior da sprint (igual a sprint_metrics.py)
    contents = sm.fetch_sprint_report_contents(board_id, sprint["id"])
    parent_keys = sm.all_parent_keys(contents)
    registered_subtasks = sm.fetch_subtasks_for_parents(parent_keys)
    registered_keys = {s["key"] for s in registered_subtasks}
    print(f"Escopo registrado: {len(parent_keys)} issues de nível superior, {len(registered_subtasks)} Subtarefas")

    # Candidatos de fora: sem sprint, com movimento para status de conclusão/revisão desde a referência
    status_list = ",".join(f'"{s}"' for s in movement_statuses)
    jql = (
        f'project = "{PROJECT_KEY}" AND sprint is EMPTY '
        f'AND status changed TO ({status_list}) AFTER "{desde_dt.strftime("%Y-%m-%d")}"'
    )
    candidates = fetch_all_issues(jql, "created,issuetype,status,parent")
    candidates = [c for c in candidates if c["fields"]["issuetype"]["name"] != EPIC_TYPE]
    print(f"Candidatos sem sprint com movimento desde {desde_dt.date()}: {len(candidates)}")

    # Mantém só os que têm o primeiro movimento dentro do período desta sprint
    in_window = []
    for c in candidates:
        transitions, _ = sm.extract_transitions(fetch_issue_changelog(c["key"]))
        anchor = first_anchor(transitions, desde_dt, done_statuses)
        if anchor is not None and start_dt <= anchor <= end_dt:
            in_window.append(c)
    print(f"Com o primeiro movimento dentro da sprint: {len(in_window)}")

    non_subtasks = [c["key"] for c in in_window if c["fields"]["issuetype"]["name"] != SUBTASK_TYPE]
    has_subtasks = set()
    for part in chunks(non_subtasks):
        for sub in fetch_all_issues("parent in (" + ",".join(part) + ")", "parent"):
            has_subtasks.add(sub["fields"]["parent"]["key"])

    extras = []
    for c in in_window:
        key, issuetype = c["key"], c["fields"]["issuetype"]["name"]
        if issuetype == SUBTASK_TYPE:
            if key not in registered_keys:  # senão já está contado via issue pai da sprint
                extras.append((key, "subtarefa_fora_sprint", c["fields"]))
        elif key not in has_subtasks and key not in parent_keys:  # com Subtarefas: representado por elas
            extras.append((key, "avulso_fora_sprint", c["fields"]))
    print(f"Incluídos: {sum(1 for e in extras if e[1] == 'subtarefa_fora_sprint')} Subtarefas e "
          f"{sum(1 for e in extras if e[1] == 'avulso_fora_sprint')} cards avulsos de fora da sprint")

    cards = []
    for sub in registered_subtasks:
        cards.append(build_card(sub["key"], "registrado", sub["fields"], sprint["name"], cutoff_dt, end_dt, status_category_map))
    for key, origem, fields in extras:
        cards.append(build_card(key, origem, fields, sprint["name"], cutoff_dt, end_dt, status_category_map))

    df = pd.DataFrame(cards)
    scenarios = {
        "A - só o registrado": scenario_metrics(df[df["origem"] == "registrado"]),
        "B - + Subtarefas fora de sprint": scenario_metrics(df[df["origem"].isin(["registrado", "subtarefa_fora_sprint"])]),
        "C - + avulsos fora de sprint": scenario_metrics(df),
        "só os incluídos de fora": scenario_metrics(df[df["origem"] != "registrado"]),
    }
    resumo = pd.DataFrame(scenarios)

    pd.set_option("display.width", 200)
    print()
    print(resumo.to_string(float_format=lambda v: f"{v:.3f}"))

    extra_note = sorted(sm._extra_done)
    meta = pd.DataFrame([
        {"campo": "sprint", "valor": sprint["name"]},
        {"campo": "periodo", "valor": f"{start_dt} até {end_dt}"},
        {"campo": "referencia (--desde)", "valor": str(desde_dt)},
        {"campo": "EXTRA_DONE_STATUSES", "valor": ", ".join(extra_note) or "(nenhum)"},
        {"campo": "nota", "valor": "Só entram cards de fora que progrediram: conclusão é teto e desvio é piso."},
    ])
    os.makedirs("output", exist_ok=True)
    output_path = f"output/simulacao_fora_sprint_{sprint['name'].replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        resumo.reset_index(names="metrica").to_excel(writer, sheet_name="resumo", index=False)
        df.to_excel(writer, sheet_name="cards", index=False)
        meta.to_excel(writer, sheet_name="meta", index=False)
    print(f"\nExcel gerado em: {output_path}")


if __name__ == "__main__":
    main()
