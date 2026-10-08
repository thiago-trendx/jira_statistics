# Métricas de sprint — definições e metodologia

Este documento descreve as métricas calculadas por [sprint_metrics.py](sprint_metrics.py) para o projeto GOPER, as decisões de metodologia por trás delas e as limitações conhecidas. A numeração (3.1.1, 3.2.1, ...) segue a spec de métricas da equipe; a spec original não está neste repositório, então este arquivo é a referência de como cada métrica está **implementada**.

> **Branch:** o que está descrito aqui reflete a branch `analise_ponderada` (visão ajustada para apresentação). Na `main`, "concluído" considera só a statusCategory "Itens concluídos" e a métrica 3.5 (tempo em revisão) ainda existe. Veja [Diferenças em relação à main](#diferenças-em-relação-à-main).

## Decisões de metodologia (confirmadas com o time)

| Decisão | Escolha | Motivo |
|---|---|---|
| Granularidade | **Subtarefa** (issue types `10003` e `10110`) | Dois tipos distintos têm o mesmo nome "Subtarefa" nesta instância; filtrar por nome retorna zero resultados, sempre filtrar por ID. |
| Unidade de escopo | **Contagem de cards** | Story Points nunca são preenchidos; estimativa de tempo é raramente preenchida em Subtarefas. |
| Calendário | **Horas úteis**: seg-sex, 9h-18h, sem feriados nacionais BR | Ver [business_hours.py](business_hours.py). Timestamps são normalizados para `America/Sao_Paulo` antes de comparar. |
| "Planejado no início" | Presente na sprint até **5 dias corridos** após o início oficial (`PLANNING_WINDOW_DAYS`) | Os dados mostraram replanejamento em lote poucos dias depois do "Iniciar Sprint" (ex.: 36 issues adicionadas no mesmo instante, ~4,3 dias depois, na sprint Copa), não deriva gradual de escopo. |
| Detecção de etapa | Por **statusCategory** (buscada ao vivo na API), não por nome literal de status | O workflow já foi migrado uma vez e invalidou nomes hardcoded. |

## Como o escopo é determinado

1. A sprint é localizada pelo nome (match exato, sem diferenciar maiúsculas) no board scrum do projeto.
2. A API interna **Greenhopper** (`sprintreport`) lista as issues de nível superior (História/Bug/Task/Epic) que passaram pela sprint. Ela não cobre Subtarefas.
3. Todas as Subtarefas cujo `parent` está nesse conjunto são buscadas.
4. Cada Subtarefa é classificada reconstruindo o valor do campo `Sprint` ao longo do tempo a partir do seu changelog e comparando dois instantes:
   - **Corte de planejamento** = início oficial da sprint + 5 dias
   - **Fim da sprint** = `completeDate` (ou `endDate`; se a sprint está ativa, o momento atual)

| No corte de planejamento | No fim da sprint | Classificação |
|---|---|---|
| Sim | Sim | `planejada` |
| Sim | Não | `removida` |
| Não | Sim | `adicionada` |
| Não | Não | usa o fallback |

**Fallback** (Subtarefa sem changelog próprio do campo `Sprint`, comum porque o campo é herdado do pai na criação sem gerar evento): `planejada` se a Subtarefa foi **criada** até o corte de planejamento, senão `adicionada`. Na `main`, o fallback usa a classificação da História-mãe.

**Escopo final** = `planejada` + `adicionada` (as `removida` ficam fora das métricas, só aparecem na aba `cards_removidos`).

## Definição de "entregue"

Um card conta como entregue se o seu **status no fim da sprint** (reconstruído pelo changelog) tem statusCategory `Itens concluídos` (hoje só `Pronto`) **ou** é um dos status pós-desenvolvimento do fluxo antigo: `QA`, `HOMOLOGAÇÃO`, `PRONTO P/ TESTES`, `PRONTO P/ PROD`.

Motivo: o trabalho fica "pronto" na prática quando o dev termina, antes de QA/homologação/deploy, e cards parados nesses status intermediários distorciam a análise de conclusão.

Para uma sprint ainda ativa na véspera da revisão, a variável de ambiente opcional `EXTRA_DONE_STATUSES` (lista separada por vírgula) conta status adicionais como concluídos naquela execução, por exemplo `EXTRA_DONE_STATUSES="Em Revisao"`. Sem a variável, o comportamento é o descrito acima. Isso também antecipa o fim do lead time (3.4) para o momento em que o card entra nesse status, então o lead time de uma execução com a variável não é comparável com as demais.

## Métricas

| ID | Nome | Fórmula | Base (n) |
|---|---|---|---|
| 3.1.1 | Conclusão do escopo total | entregues / (planejadas + adicionadas) | escopo final |
| 3.1.2 | Conclusão do escopo planejado | entregues entre as planejadas / planejadas | planejadas |
| 3.2.1 | Desvio de escopo | adicionadas / planejadas | planejadas |
| 3.2.2 | Precisão da estimativa | tempo gasto total / tempo estimado total, só cards com os dois campos | cards com ambos os campos |
| 3.3.1 | % de cards bloqueados ao final | cards em `Bloqueado` no fim da sprint (escopo final) / planejadas | planejadas |
| 3.3.2 | Tempo total bloqueado (h úteis) | soma dos intervalos em `Bloqueado` de todos os cards do escopo final | escopo final |
| 3.4 | Lead time médio e mediano (h úteis) | da 1ª saída de "Itens Pendentes" até o 1º status de "entregue" | cards com lead time calculável |
| 3.5 | Tempo em revisão | **removida** nesta branch; será obtida via GitHub | — |
| 3.6 | Taxa de retrabalho | transições `Em Revisao` → {`CORREÇÃO`, `Reprovado`, `ANÁLISE`} / entradas em `Em Revisao` | total de entradas em revisão |

Linhas extras no resumo (não vêm da spec):

- **Cards que passaram por Bloqueado** — contagem de cards com ao menos uma entrada em `Bloqueado`.
- **Tempo médio de bloqueio (dias úteis)** — média do tempo bloqueado entre esses cards, dividida por 9 h/dia útil.
- **Cards concluídos** — contagem absoluta de entregues no escopo final.
- **Regime de workflow** e **nota metodológica** (metadados).

### Detalhes de cálculo

- **3.2.1 e 3.3.1 têm denominador diferente do numerador.** A divisão é sempre por `planejadas`, não pelo escopo final. Um desvio de 0,5 significa "50% do tamanho do planejado foi adicionado".
- **3.3.2:** um intervalo de bloqueio ainda aberto no fim da sprint é fechado no fim da sprint. Bloqueios anteriores ao início da sprint também entram, pois o cálculo é sobre o histórico inteiro do card.
- **3.4 Lead time:** o início é a primeira transição para um status cuja statusCategory não é "Itens Pendentes". Cards que nunca foram entregues, ou que têm entrega sem início identificável, ficam sem lead time e fora da média/mediana.
- **3.6 Retrabalho:** `ANÁLISE` é dual (etapa anterior ao dev e destino de retrabalho pós-revisão no fluxo novo). Só conta como retrabalho se a transição **sair de `Em Revisao`**.
- **3.2.2:** na prática tem base pequena ou vazia, porque estimativas quase não são preenchidas nas Subtarefas. Olhar sempre o `base_n`.

## Dois regimes de workflow

Por volta de **jun/2026** o time migrou de um fluxo granular para um simplificado.

- **Antigo:** `Backlog/A Fazer → DESENVOLVIMENTO/Em progresso → Em Revisao → QA → HOMOLOGAÇÃO → ANÁLISE → PRONTO P/ TESTES → PRONTO P/ PROD → Pronto`, mais `Bloqueado`, `CORREÇÃO`, `AGUARDANDO CORREÇÃO`, `Reprovado`.
- **Novo:** `Backlog`, `Bloqueado`, `EM ANDAMENTO`, `Em Revisao`, `ANÁLISE`, `Pronto`.

O script marca cada sprint com `regime_workflow = "antigo"` se algum status aposentado (`OLD_REGIME_STATUSES`) aparece no changelog dos cards. **Lead time de sprints no regime antigo tende a ser inflado** por cards parados em status intermediários hoje extintos, e não é diretamente comparável com sprints do regime novo.

Se o workflow mudar de novo, estender os conjuntos de aliases em `sprint_metrics.py` (`EFFECTIVE_DONE_STATUSES`, `REWORK_TARGET_STATUSES`, `OLD_REGIME_STATUSES`) em vez de criar ramificações por nome de status em outros pontos.

## Saída

`output/sprint_metrics_<sprint>_<timestamp>.xlsx`, com as abas:

- `resumo` — uma linha por métrica (`metrica`, `nome`, `valor`, `formula`, `base_n`)
- `cards_planejados`, `cards_adicionados`, `cards_removidos` — cards por classificação
- `fluxo_detalhe` — escopo final com `status_no_fim_sprint`, `entregue`, `bloqueado_no_fim`, `lead_time_horas`, `tempo_bloqueado_horas`, `vezes_bloqueado`, `ciclos_revisao`, `retrabalhos` e estimativa/tempo gasto

A pasta `output/` está no `.gitignore`: os exports não são commitados.

## Limitações conhecidas

- A API Greenhopper é interna e não documentada; pode mudar sem aviso.
- Sprint **ativa** gera snapshot parcial (fim = agora); o script avisa no console.
- O nome da sprint precisa bater exatamente; uma sprint por execução.
- O dashboard em `dashboard/` tem os dados embutidos no HTML. Atualizar para novas sprints exige rodar o script por sprint e refazer o objeto de dados manualmente.
- Sem dados de revisão de código: o tempo em revisão (3.5) depende de integração futura com o GitHub.

## Diferenças em relação à main

| Aspecto | `main` | `analise_ponderada` |
|---|---|---|
| "Concluído" | só statusCategory `Itens concluídos` | inclui também QA, HOMOLOGAÇÃO, PRONTO P/ TESTES, PRONTO P/ PROD |
| 3.5 Tempo em revisão | presente | removida |
| Fallback de classificação de escopo | classificação da História-mãe | data de criação da própria Subtarefa |
| Linhas extras no resumo | não | bloqueados (qtd e tempo médio em dias úteis) e cards concluídos |
