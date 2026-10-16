"""
ambulatorio.py

Dados da página Ambulatório.

Atendimento ambulatorial = t_internacao com isn_tipo_atendimento = 4, datado
por dat_cadastro. Especialidade e status vêm da marcação
ligada ao atendimento (t_marcacao.isn_internacao): em set/2026 todos os
atendimentos tinham exatamente uma marcação.

Desempenho: t_marcacao (~670 MB) não tem índice em isn_internacao, então
cruzar com ela exige ler a tabela inteira (≈5 s com cache quente, mais de
2 min com cache frio). Por isso a consulta busca, uma vez por período, um
resumo agrupado (dia × hora × especialidade × status) que fica em
memória por alguns minutos; os filtros de especialidade e status são
aplicados sobre esse resumo, sem nova consulta ao banco.
"""

import statistics
import threading
import time
from datetime import date, timedelta

from indicadores_hcasg import fetch_all, _DIAS_SEMANA

_CACHE_TTL = 600  # segundos
_cache = {}
_cache_lock = threading.Lock()


def _resumo_periodo(dt_ini, dt_fim):
    """Linhas agrupadas do período (com cache em memória)."""
    chave = (dt_ini, dt_fim)
    agora = time.monotonic()
    with _cache_lock:
        item = _cache.get(chave)
        if item and agora - item[0] < _CACHE_TTL:
            return item[1]

    rows = fetch_all("""
        WITH atend AS MATERIALIZED (
            SELECT ti.isn_internacao, ti.dat_cadastro
            FROM t_internacao ti
            WHERE ti.isn_tipo_atendimento = 4
              AND ti.dat_cadastro::date BETWEEN %s AND %s
        ),
        marcacao AS MATERIALIZED (
            -- uma marcação por atendimento (a mais recente, se houver mais)
            SELECT DISTINCT ON (tm.isn_internacao)
                tm.isn_internacao,
                tm.isn_especialidade,
                tm.dsc_especialidade,
                tm.isn_marcacao_situacao
            FROM t_marcacao tm
            WHERE tm.isn_internacao IN (SELECT isn_internacao FROM atend)
            ORDER BY tm.isn_internacao, tm.isn_marcacao DESC
        )
        SELECT
            a.dat_cadastro::date                          AS dia,
            EXTRACT(HOUR FROM a.dat_cadastro)::int        AS hora,
            COALESCE(NULLIF(TRIM(te.dsc_especialidade), ''),
                     NULLIF(TRIM(m.dsc_especialidade), ''),
                     'Sem especialidade')                 AS especialidade,
            COALESCE(tms.dsc_marcacao_situacao, 'Sem marcação') AS status,
            COUNT(*)                                      AS qtd
        FROM atend a
        LEFT JOIN marcacao m ON m.isn_internacao = a.isn_internacao
        LEFT JOIN t_especialidade te ON te.isn_especialidade = m.isn_especialidade
        LEFT JOIN t_marcacao_situacao tms ON tms.isn_marcacao_situacao = m.isn_marcacao_situacao
        GROUP BY 1, 2, 3, 4
    """, [dt_ini, dt_fim])
    resumo = [
        {
            "dia": r["dia"],
            "hora": r["hora"],
            "especialidade": r["especialidade"].strip(),
            "status": r["status"].strip(),
            "qtd": int(r["qtd"]),
        }
        for r in rows or []
    ]

    with _cache_lock:
        # descarta entradas vencidas para o cache não crescer sem limite
        for k in [k for k, v in _cache.items() if agora - v[0] >= _CACHE_TTL]:
            del _cache[k]
        _cache[chave] = (agora, resumo)
    return resumo


def _contar(linhas, campo):
    tot = {}
    for r in linhas:
        tot[r[campo]] = tot.get(r[campo], 0) + r["qtd"]
    return sorted(({"nome": k, "qtd": v} for k, v in tot.items()), key=lambda x: (-x["qtd"], x["nome"]))


def get_ambulatorio(data_ini, data_fim, especialidade=None, status=None):
    dt_ini = date.fromisoformat(data_ini)
    dt_fim = date.fromisoformat(data_fim)
    if dt_ini > dt_fim:
        dt_ini, dt_fim = dt_fim, dt_ini

    resumo = _resumo_periodo(dt_ini, dt_fim)

    # opções dos filtros: tudo o que aparece no período (sem filtro aplicado)
    opcoes = {
        "especialidades": sorted({r["especialidade"] for r in resumo}),
        "status": [x["nome"] for x in _contar(resumo, "status")],
    }

    linhas = [
        r for r in resumo
        if (not especialidade or r["especialidade"] == especialidade)
        and (not status or r["status"] == status)
    ]

    dias = [dt_ini + timedelta(days=i) for i in range((dt_fim - dt_ini).days + 1)]
    idx_dia = {d: i for i, d in enumerate(dias)}
    por_dia = [0] * len(dias)
    por_hora = [0] * 24
    for r in linhas:
        por_dia[idx_dia[r["dia"]]] += r["qtd"]
        if r["hora"] is not None:
            por_hora[r["hora"]] += r["qtd"]

    # dias sem nenhum atendimento no ambulatório (sábados, domingos e feriados)
    # ficam fora da média e da mediana; o critério é o total do ambulatório,
    # sem os filtros, para uma especialidade que não atendeu num dia útil
    # continuar contando aquele dia
    atendeu = [False] * len(dias)
    for r in resumo:
        if r["qtd"]:
            atendeu[idx_dia[r["dia"]]] = True
    por_dia_util = [qtd for qtd, aberto in zip(por_dia, atendeu) if aberto]

    totais_semana = [0] * 7
    ocorrencias_semana = [0] * 7
    for d, qtd, aberto in zip(dias, por_dia, atendeu):
        totais_semana[d.weekday()] += qtd
        if aberto:
            ocorrencias_semana[d.weekday()] += 1

    por_status = _contar(linhas, "status")
    total = sum(por_dia)
    qtd_status = {x["nome"].upper(): x["qtd"] for x in por_status}
    concluidos = qtd_status.get("CONCLUÍDO", 0) + qtd_status.get("ALTA AMBULATORIAL", 0)
    faltas = qtd_status.get("FALTOU", 0)

    return {
        "opcoes": opcoes,
        "kpis": {
            "total": total,
            "media_diaria": round(total / len(por_dia_util), 1) if por_dia_util else None,
            "dias_com_atendimento": len(por_dia_util),
            "dias_sem_atendimento": len(dias) - len(por_dia_util),
            "concluidos": concluidos,
            "faltas": faltas,
            "taxa_absenteismo": round(faltas / total * 100, 1) if total else None,
            "especialidades": len({r["especialidade"] for r in linhas}),
        },
        "dias": [d.strftime("%d/%m/%Y") for d in dias],
        "por_dia": por_dia,
        "mediana_diaria": round(statistics.median(por_dia_util), 1) if por_dia_util else None,
        "dias_semana": _DIAS_SEMANA,
        "por_dia_semana": totais_semana,
        "media_dia_semana": [
            round(t / o, 1) if o else None for t, o in zip(totais_semana, ocorrencias_semana)
        ],
        "horas": [f"{h:02d}h" for h in range(24)],
        "por_hora": por_hora,
        "por_especialidade": _contar(linhas, "especialidade"),
        "por_status": por_status,
    }
