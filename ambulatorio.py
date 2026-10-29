"""
ambulatorio.py

Dados da página Ambulatório.

Atendimento ambulatorial = t_internacao com isn_tipo_atendimento = 4, datado
por dat_cadastro. Especialidade e status vêm da marcação
ligada ao atendimento (t_marcacao.isn_internacao): em set/2026 todos os
atendimentos tinham exatamente uma marcação.

Desempenho: sem índices, a consulta lia t_marcacao (~670 MB) e t_internacao
(~230 MB) inteiras (mais de 1 min com o banco frio). Em 28/10/2026 foram
criados no banco hospitalar:

  CREATE INDEX CONCURRENTLY idx_t_marcacao_isn_internacao ON t_marcacao (isn_internacao);
  CREATE INDEX CONCURRENTLY idx_t_internacao_dat_cadastro ON t_internacao (dat_cadastro);

(um ano em ~2 s). O filtro de data fica na própria coluna (sem ::date) para
usar o índice. Num servidor novo, sem esses índices, a página volta a ficar lenta.
A consulta busca, uma vez por período, um resumo agrupado (dia ×
especialidade × status) que fica em memória por alguns minutos; os filtros
de especialidade e status são aplicados sobre esse resumo, sem nova
consulta ao banco.

Horário e tempo das consultas: vêm do registro do atendimento médico
(t_internacao_atendimento), com a data (dat_ini/dat_fim) e a hora em texto
"HH:MM" (dsc_hor_ini/dsc_hor_fim) de início e de fim. Só a consulta realizada
tem esse registro (falta e cancelamento não). A tabela não tem índice em
dat_ini nem em isn_internacao (leitura inteira, ~1 s); a marcação de cada
consulta é buscada com LATERAL pelo índice de t_marcacao.isn_internacao,
porque o planejador subestima o período em dat_ini e, juntando as CTEs,
caía num laço de milhões de comparações.
"""

import statistics
import threading
import time
from datetime import date, timedelta

import banco_painel
from indicadores_hcasg import fetch_all, _DIAS_SEMANA

_CACHE_TTL = 600  # segundos
_cache = {}
_cache_lock = threading.Lock()


def _do_cache(chave):
    with _cache_lock:
        item = _cache.get(chave)
        if item and time.monotonic() - item[0] < _CACHE_TTL:
            return item[1]
    return None


def _guardar(chave, valor):
    agora = time.monotonic()
    with _cache_lock:
        # descarta entradas vencidas para o cache não crescer sem limite
        for k in [k for k, v in _cache.items() if agora - v[0] >= _CACHE_TTL]:
            del _cache[k]
        _cache[chave] = (agora, valor)
    return valor


# atendimentos do período e a marcação de cada um (especialidade e status)
_CTE_ATENDIMENTOS = """
        WITH atend AS MATERIALIZED (
            SELECT ti.isn_internacao, ti.dat_cadastro
            FROM t_internacao ti
            WHERE ti.isn_tipo_atendimento = 4
              -- intervalo na própria coluna (sem ::date) para poder usar índice em dat_cadastro
              AND ti.dat_cadastro >= %s AND ti.dat_cadastro < %s
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
        )"""
_ESPECIALIDADE_STATUS = """
            COALESCE(NULLIF(TRIM(te.dsc_especialidade), ''),
                     NULLIF(TRIM(m.dsc_especialidade), ''),
                     'Sem especialidade')                 AS especialidade,
            COALESCE(tms.dsc_marcacao_situacao, 'Sem marcação') AS status,"""
_JOINS_ESPECIALIDADE_STATUS = """
        LEFT JOIN marcacao m ON m.isn_internacao = a.isn_internacao
        LEFT JOIN t_especialidade te ON te.isn_especialidade = m.isn_especialidade
        LEFT JOIN t_marcacao_situacao tms ON tms.isn_marcacao_situacao = m.isn_marcacao_situacao"""


def _resumo_periodo(dt_ini, dt_fim):
    """Linhas agrupadas do período (com cache em memória)."""
    chave = (dt_ini, dt_fim)
    resumo = _do_cache(chave)
    if resumo is not None:
        return resumo

    rows = fetch_all(f"""{_CTE_ATENDIMENTOS}
        SELECT
            a.dat_cadastro::date                          AS dia,{_ESPECIALIDADE_STATUS}
            COUNT(*)                                      AS qtd
        FROM atend a{_JOINS_ESPECIALIDADE_STATUS}
        GROUP BY 1, 2, 3
    """, [dt_ini, dt_fim + timedelta(days=1)])
    return _guardar(chave, [
        {
            "dia": r["dia"],
            "especialidade": r["especialidade"].strip(),
            "status": r["status"].strip(),
            "qtd": int(r["qtd"]),
        }
        for r in rows or []
    ])


_HORA_VALIDA = "'^([01]?[0-9]|2[0-3]):[0-5][0-9]$'"
_MINUTOS_MAXIMO = 12 * 60  # consulta mais longa que isso é erro de digitação da hora


def _consultas_periodo(dt_ini, dt_fim):
    """Um registro por consulta realizada no período (data de início em
    t_internacao_atendimento.dat_ini): (especialidade, status, hora de início,
    duração em minutos). Hora fora do formato HH:MM, sem fim ou duração fora
    de 0 a 12 h: None (com cache em memória)."""
    chave = ('consultas', dt_ini, dt_fim)
    consultas = _do_cache(chave)
    if consultas is not None:
        return consultas

    rows = fetch_all(f"""
        WITH atend AS MATERIALIZED (
            SELECT x.isn_internacao, x.dat_ini, x.dsc_hor_ini, x.dat_fim, x.dsc_hor_fim
            FROM t_internacao_atendimento x
            JOIN t_internacao ti ON ti.isn_internacao = x.isn_internacao AND ti.isn_tipo_atendimento = 4
            WHERE x.dat_ini >= %s AND x.dat_ini < %s
        )
        SELECT{_ESPECIALIDADE_STATUS}
            CASE WHEN a.dsc_hor_ini ~ {_HORA_VALIDA}
                 THEN split_part(a.dsc_hor_ini, ':', 1)::int END AS hora,
            CASE WHEN a.dsc_hor_ini ~ {_HORA_VALIDA} AND a.dsc_hor_fim ~ {_HORA_VALIDA} AND a.dat_fim IS NOT NULL
                 THEN EXTRACT(EPOCH FROM (a.dat_fim::date + a.dsc_hor_fim::time)
                                       - (a.dat_ini::date + a.dsc_hor_ini::time)) / 60 END AS minutos
        FROM atend a
        LEFT JOIN LATERAL (
            -- a marcação mais recente da consulta, pelo índice de t_marcacao.isn_internacao
            SELECT tm.isn_especialidade, tm.dsc_especialidade, tm.isn_marcacao_situacao
            FROM t_marcacao tm
            WHERE tm.isn_internacao = a.isn_internacao
            ORDER BY tm.isn_marcacao DESC
            LIMIT 1
        ) m ON TRUE
        LEFT JOIN t_especialidade te ON te.isn_especialidade = m.isn_especialidade
        LEFT JOIN t_marcacao_situacao tms ON tms.isn_marcacao_situacao = m.isn_marcacao_situacao
    """, [dt_ini, dt_fim + timedelta(days=1)])
    return _guardar(chave, [
        (r["especialidade"].strip(), r["status"].strip(), r["hora"],
         float(r["minutos"]) if r["minutos"] is not None and 0 <= r["minutos"] <= _MINUTOS_MAXIMO else None)
        for r in rows or []
    ])


def tempo_consulta(consultas, especialidade=None, status=None):
    """Tempo médio e mediano (em minutos) por consulta, por especialidade (da
    mais demorada para a mais rápida), e o total. Só entram as consultas com
    início e fim válidos."""
    tempos = {}
    for esp, st, _, minutos in consultas:
        if minutos is None or (especialidade and esp != especialidade) or (status and st != status):
            continue
        tempos.setdefault(esp, []).append(minutos)

    def linha(nome, valores):
        return {"nome": nome, "consultas": len(valores),
                "media": round(statistics.mean(valores), 1), "mediana": round(statistics.median(valores), 1)}

    todos = [m for v in tempos.values() for m in v]
    return {
        "especialidades": sorted((linha(e, v) for e, v in tempos.items()),
                                 key=lambda x: (-x["media"], x["nome"])),
        "total": linha("Total", todos) if todos else None,
    }


def _visitas_periodo(dt_ini, dt_fim):
    """Um registro por atendimento: (paciente, especialidade, status, dia,
    município, sexo, idade), para a tabela por prontuário e os atendimentos
    por município, sexo e idade (com cache em memória). O município é o do atendimento
    (t_internacao.isn_municipio), com a UF: há nomes iguais em estados diferentes.
    O sexo vem do cadastro do paciente (t_paciente.dsc_sexo); a idade, em anos
    completos na data do atendimento, de t_paciente.dat_nascimento (sem data
    ou fora de 0 a 120 anos: None)."""
    chave = ('visitas', dt_ini, dt_fim)
    visitas = _do_cache(chave)
    if visitas is not None:
        return visitas

    rows = fetch_all(f"""{_CTE_ATENDIMENTOS}
        SELECT ti.isn_paciente AS paciente,{_ESPECIALIDADE_STATUS}
            a.dat_cadastro::date AS dia,
            COALESCE(NULLIF(TRIM(tmu.dsc_municipio), '') || COALESCE('/' || NULLIF(TRIM(tuf.sgl_uf), ''), ''),
                     'Sem município') AS municipio,
            COALESCE(NULLIF(TRIM(tp.dsc_sexo), ''), 'Não informado') AS sexo,
            date_part('year', age(a.dat_cadastro::date, tp.dat_nascimento::date))::int AS idade
        FROM atend a
        JOIN t_internacao ti ON ti.isn_internacao = a.isn_internacao{_JOINS_ESPECIALIDADE_STATUS}
        LEFT JOIN t_municipio tmu ON tmu.isn_municipio = ti.isn_municipio
        LEFT JOIN t_uf tuf ON tuf.isn_uf = ti.isn_uf
        LEFT JOIN t_paciente tp ON tp.isn_paciente = ti.isn_paciente
    """, [dt_ini, dt_fim + timedelta(days=1)])
    return _guardar(chave, [
        (r["paciente"], r["especialidade"].strip(), r["status"].strip(), r["dia"], r["municipio"].strip(),
         r["sexo"].strip(), r["idade"] if r["idade"] is not None and 0 <= r["idade"] <= _IDADE_MAXIMA else None)
        for r in rows or []
    ])


def por_municipio(visitas, especialidade=None, status=None):
    """Atendimentos e prontuários únicos por município, do maior para o menor."""
    atend, pacientes = {}, {}
    for paciente, esp, st, _, municipio, *_ in visitas:
        if (especialidade and esp != especialidade) or (status and st != status):
            continue
        atend[municipio] = atend.get(municipio, 0) + 1
        if paciente is not None:
            pacientes.setdefault(municipio, set()).add(paciente)
    return sorted(({"nome": m, "qtd": q, "prontuarios": len(pacientes.get(m, ()))} for m, q in atend.items()),
                  key=lambda x: (-x["qtd"], x["nome"]))


def por_macrorregiao(visitas, macro_do_municipio, especialidade=None, status=None):
    """Atendimentos e prontuários únicos por macrorregião de saúde do Ceará
    (cadastro Regiões de Saúde: {município sem acento: macrorregião}).
    Município do Ceará fora do cadastro: "Ceará (não classificado)"; de outro
    estado: "Outros estados"."""
    atend, pacientes = {}, {}
    for paciente, esp, st, _, municipio, *_ in visitas:
        if (especialidade and esp != especialidade) or (status and st != status):
            continue
        nome, _, uf = municipio.rpartition('/')
        if not nome:
            grupo = municipio  # "Sem município"
        elif uf != 'CE':
            grupo = 'Outros estados'
        else:
            grupo = macro_do_municipio.get(banco_painel._chave_nome(nome)) or 'Ceará (não classificado)'
        atend[grupo] = atend.get(grupo, 0) + 1
        if paciente is not None:
            pacientes.setdefault(grupo, set()).add(paciente)
    return sorted(({"nome": g, "qtd": q, "prontuarios": len(pacientes.get(g, ()))} for g, q in atend.items()),
                  key=lambda x: (-x["qtd"], x["nome"]))


_ORDEM_SEXO = ('Feminino', 'Masculino')


def por_sexo(visitas, especialidade=None, status=None):
    """Atendimentos por sexo do paciente: no total (com prontuários únicos) e
    em cada especialidade (da que tem mais atendimentos para a que tem menos).
    Feminino e Masculino primeiro; "Não informado" e outros valores depois."""
    total, pacientes, por_esp = {}, {}, {}
    for paciente, esp, st, _, _, sexo, *_ in visitas:
        if (especialidade and esp != especialidade) or (status and st != status):
            continue
        total[sexo] = total.get(sexo, 0) + 1
        if paciente is not None:
            pacientes.setdefault(sexo, set()).add(paciente)
        contagem = por_esp.setdefault(esp, {})
        contagem[sexo] = contagem.get(sexo, 0) + 1
    sexos = [s for s in _ORDEM_SEXO if s in total] + sorted(s for s in total if s not in _ORDEM_SEXO)
    return {
        "sexos": sexos,
        "total": [{"nome": s, "qtd": total[s], "prontuarios": len(pacientes.get(s, ()))} for s in sexos],
        "por_especialidade": sorted(
            ({"nome": e, "qtd": sum(c.values()), "sexos": [c.get(s, 0) for s in sexos]} for e, c in por_esp.items()),
            key=lambda x: (-x["qtd"], x["nome"])),
    }


_IDADE_MAXIMA = 120


def por_idade(visitas, especialidade=None, status=None):
    """Atendimentos e prontuários únicos por idade (anos completos na data do
    atendimento), um item por ano de 0 até a maior idade do período (anos sem
    atendimento ficam com zero), com a idade média e a mediana. "Não
    informada" só aparece quando há atendimento sem idade."""
    atend, pacientes, idades, sem_idade, sem_idade_pac = {}, {}, [], 0, set()
    for paciente, esp, st, _, _, _, idade, *_ in visitas:
        if (especialidade and esp != especialidade) or (status and st != status):
            continue
        if idade is None:
            sem_idade += 1
            if paciente is not None:
                sem_idade_pac.add(paciente)
            continue
        atend[idade] = atend.get(idade, 0) + 1
        idades.append(idade)
        if paciente is not None:
            pacientes.setdefault(idade, set()).add(paciente)
    linhas = [{"nome": str(i), "qtd": atend.get(i, 0), "prontuarios": len(pacientes.get(i, ()))}
              for i in range(max(atend) + 1 if atend else 0)]
    if sem_idade:
        linhas.append({"nome": "Não informada", "qtd": sem_idade, "prontuarios": len(sem_idade_pac)})
    return {
        "idades": linhas,
        "media": round(statistics.mean(idades), 1) if idades else None,
        "mediana": statistics.median(idades) if idades else None,
    }


_DIAS_POR_MES = 30.44


def por_prontuario(visitas, especialidade=None, status=None):
    """Por especialidade: atendimentos, prontuários (pacientes) únicos,
    atendimentos por prontuário, prontuários com retorno e o intervalo médio
    em meses entre atendimentos consecutivos do mesmo paciente na
    especialidade (só dentro do período; dias repetidos contam uma vez).
    Retorna (linhas da maior para a menor, total)."""
    datas = {}
    for paciente, esp, st, dia, *_ in visitas:
        if paciente is None or (especialidade and esp != especialidade) or (status and st != status):
            continue
        datas.setdefault(esp, {}).setdefault(paciente, []).append(dia)

    def linha(nome, pacientes):
        atend = sum(len(d) for d in pacientes.values())
        intervalos = []
        for dias in pacientes.values():
            dias = sorted(set(dias))
            intervalos += [(b - a).days for a, b in zip(dias, dias[1:])]
        return {
            "nome": nome,
            "atendimentos": atend,
            "prontuarios": len(pacientes),
            "por_prontuario": round(atend / len(pacientes), 2) if pacientes else None,
            "com_retorno": sum(1 for d in pacientes.values() if len(set(d)) > 1),
            "intervalo_meses": round(statistics.mean(intervalos) / _DIAS_POR_MES, 1) if intervalos else None,
            "_intervalos": intervalos,
        }

    linhas = sorted((linha(esp, pac) for esp, pac in datas.items()),
                    key=lambda x: (-x["atendimentos"], x["nome"]))
    # total: prontuário único no geral (o mesmo paciente em duas especialidades conta uma vez);
    # intervalo médio de todos os retornos dentro de cada especialidade
    todos = {}
    for pac in datas.values():
        for p, d in pac.items():
            todos.setdefault(p, []).extend(d)
    intervalos = [i for l in linhas for i in l.pop("_intervalos")]
    total = {
        "nome": "Total",
        "atendimentos": sum(l["atendimentos"] for l in linhas),
        "prontuarios": len(todos),
        "com_retorno": sum(l["com_retorno"] for l in linhas),
        "intervalo_meses": round(statistics.mean(intervalos) / _DIAS_POR_MES, 1) if intervalos else None,
    }
    total["por_prontuario"] = round(total["atendimentos"] / total["prontuarios"], 2) if todos else None
    return linhas, total


def _salas_periodo(dt_ini, dt_fim):
    """Atendimentos por sala × especialidade × status (com cache em memória).

    A sala é o leito do atendimento: t_internacao_leito.isn_leito ->
    t_leito.dsc_leito (SL-01, TRANSPLANTE CARDÍACO 5...; todos na enfermaria
    "Consultórios" da clínica AMBULATORIO). t_internacao_leito não tem índice
    em isn_internacao: esta consulta lê a tabela inteira (~280 MB), por isso
    é separada da principal e a página a carrega depois dos outros gráficos."""
    chave = ('salas', dt_ini, dt_fim)
    salas = _do_cache(chave)
    if salas is not None:
        return salas

    rows = fetch_all(f"""{_CTE_ATENDIMENTOS},
        leito AS MATERIALIZED (
            -- um leito por atendimento (o último, se houver mais)
            SELECT DISTINCT ON (il.isn_internacao) il.isn_internacao, il.isn_leito
            FROM t_internacao_leito il
            WHERE il.isn_internacao IN (SELECT isn_internacao FROM atend)
            ORDER BY il.isn_internacao, il.isn_internacao_leito DESC
        )
        SELECT
            COALESCE(NULLIF(TRIM(tl.dsc_leito), ''), 'Sem sala') AS sala,{_ESPECIALIDADE_STATUS}
            COUNT(*)                                      AS qtd
        FROM atend a{_JOINS_ESPECIALIDADE_STATUS}
        LEFT JOIN leito l ON l.isn_internacao = a.isn_internacao
        LEFT JOIN t_leito tl ON tl.isn_leito = l.isn_leito
        GROUP BY 1, 2, 3
    """, [dt_ini, dt_fim + timedelta(days=1)])
    return _guardar(chave, [
        {"sala": r["sala"].strip(), "especialidade": r["especialidade"].strip(),
         "status": r["status"].strip(), "qtd": int(r["qtd"])}
        for r in rows or []
    ])


def get_salas(data_ini, data_fim, especialidade=None, status=None):
    """Consultas por status em cada sala: salas da mais para a menos usada e
    uma série por status (mesmos filtros de especialidade e status da página)."""
    dt_ini, dt_fim = sorted((date.fromisoformat(data_ini), date.fromisoformat(data_fim)))
    linhas = [r for r in _salas_periodo(dt_ini, dt_fim)
              if (not especialidade or r["especialidade"] == especialidade)
              and (not status or r["status"] == status)]
    salas = [x["nome"] for x in _contar(linhas, "sala")]
    status_ordem = [x["nome"] for x in _contar(linhas, "status")]
    idx = {s: i for i, s in enumerate(salas)}
    series = {s: [0] * len(salas) for s in status_ordem}
    for r in linhas:
        series[r["status"]][idx[r["sala"]]] += r["qtd"]
    return {"salas": salas, "series": [{"status": s, "valores": series[s]} for s in status_ordem]}


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
    for r in linhas:
        por_dia[idx_dia[r["dia"]]] += r["qtd"]

    # horário: início da consulta registrado no atendimento médico
    consultas = _consultas_periodo(dt_ini, dt_fim)
    por_hora = [0] * 24
    for esp, st, hora, _ in consultas:
        if hora is not None and (not especialidade or esp == especialidade) and (not status or st == status):
            por_hora[hora] += 1

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
    visitas = _visitas_periodo(dt_ini, dt_fim)
    prontuario_linhas, prontuario_total = por_prontuario(visitas, especialidade, status)
    total = sum(por_dia)
    qtd_status = {x["nome"].upper(): x["qtd"] for x in por_status}
    altas = qtd_status.get("ALTA AMBULATORIAL", 0)
    concluidos = qtd_status.get("CONCLUÍDO", 0) + altas
    faltas = qtd_status.get("FALTOU", 0)

    return {
        "opcoes": opcoes,
        "kpis": {
            "total": total,
            "media_diaria": round(total / len(por_dia_util), 1) if por_dia_util else None,
            "dias_com_atendimento": len(por_dia_util),
            "dias_sem_atendimento": len(dias) - len(por_dia_util),
            "concluidos": concluidos,
            "altas": altas,
            "taxa_alta": round(altas / total * 100, 1) if total else None,
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
        "tempo_consulta": tempo_consulta(consultas, especialidade, status),
        "por_especialidade": _contar(linhas, "especialidade"),
        "por_status": por_status,
        "por_prontuario": prontuario_linhas,
        "por_prontuario_total": prontuario_total,
        "por_sexo": por_sexo(visitas, especialidade, status),
        "por_idade": por_idade(visitas, especialidade, status),
        "por_municipio": por_municipio(visitas, especialidade, status),
        "por_macrorregiao": por_macrorregiao(visitas, banco_painel.macrorregiao_dos_municipios(), especialidade, status),
    }
