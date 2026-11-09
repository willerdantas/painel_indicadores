"""
interconsultas.py

Dados da página Interconsultas.

Interconsulta = t_interconsulta, ligada à internação/atendimento por
isn_internacao (t_internacao: isn_tipo_atendimento 2 = emergência,
1 = internação) e à especialidade solicitada por isn_especialidade
(t_especialidade). Solicitadas contam pela data da solicitação
(dat_solicitacao) e pareceres pela data do parecer (dat_parecer): um parecer
do período pode ser de uma interconsulta solicitada antes dele. Interconsulta
cancelada (dat_cancel preenchida) não entra em nada.

Clínica, enfermaria e leito (filtros da página e tabela por clínica): os da
movimentação (t_internacao_leito) em que o paciente estava na data da
solicitação; sem movimentação até essa data, a primeira da internação.

As colunas de t_interconsulta foram assumidas (a tabela ainda não existia no
banco em 09/11/2026): COLUNAS_INTERCONSULTA. Se faltar alguma no servidor, a
página avisa qual em vez de dar erro de SQL.
"""

from datetime import date

import banco_hospitalar
from banco_hospitalar import fetch_all
from indicadores_hcasg import _add_months, _filtros_sql, _meses_iso, _resolve_month_window, _rotulos

COLUNAS_INTERCONSULTA = ('isn_internacao', 'isn_especialidade', 'dat_solicitacao', 'dat_parecer', 'dat_cancel')
TIPO_EMERGENCIA, TIPO_INTERNACAO = 2, 1
SEM_ESPECIALIDADE, SEM_CLINICA = 'Sem especialidade', 'Sem clínica'

_ENTRADA_DIA = "COALESCE(til.dat_internacao_info, til.dat_internacao)::date"
_ENTRADA = ("COALESCE(til.dat_internacao_info + til.hor_internacao_info::time, "
            "til.dat_internacao + til.hor_internacao::time)")


def colunas_faltando():
    """Colunas de COLUNAS_INTERCONSULTA que t_interconsulta não tem no servidor
    ([] se a tabela não existe: aí o aviso de tabela faltando já cobre)."""
    estrutura = banco_hospitalar.estrutura()
    if not estrutura or 't_interconsulta' not in estrutura:
        return []
    return [c for c in COLUNAS_INTERCONSULTA if c not in estrutura['t_interconsulta']]


# Interconsultas solicitadas ou com parecer no período (parâmetros: as duas
# datas, duas vezes), com o tipo de atendimento, a clínica/enfermaria/leito da
# solicitação e as horas entre a solicitação e o parecer, na CTE base.
# As interconsultas do período primeiro e as movimentações delas numa só
# passada (DISTINCT ON): buscar a movimentação interconsulta a interconsulta
# (LATERAL) lia t_internacao_leito inteira para cada uma (400 levaram 77 s).
_BASE_SQL = f"""
        WITH periodo AS MATERIALIZED (
            SELECT row_number() OVER () AS id, ic.isn_internacao, ic.isn_especialidade,
                   ic.dat_solicitacao::date AS dia_solicitacao, ic.dat_parecer::date AS dia_parecer,
                   EXTRACT(EPOCH FROM (ic.dat_parecer::timestamp - ic.dat_solicitacao::timestamp)) / 3600 AS horas
            FROM t_interconsulta ic
            WHERE ic.dat_cancel IS NULL  -- canceladas não entram em nada
              AND (ic.dat_solicitacao::date BETWEEN %s AND %s
                   OR ic.dat_parecer::date BETWEEN %s AND %s)
        ),
        mov AS (
            -- movimentação em que o paciente estava na solicitação: a última
            -- que começou até ela; sem nenhuma, a primeira da internação
            SELECT DISTINCT ON (p.id) p.id, tc.dsc_clinica, te.dsc_enfermaria, tl.dsc_leito
            FROM periodo p
            JOIN t_internacao_leito til ON til.isn_internacao = p.isn_internacao
            JOIN t_clinica tc ON tc.isn_clinica = til.isn_clinica
            LEFT JOIN t_enfermaria te ON te.isn_enfermaria = til.isn_enfermaria
            LEFT JOIN t_leito tl ON tl.isn_leito = til.isn_leito
            ORDER BY p.id,
                     {_ENTRADA_DIA} <= p.dia_solicitacao DESC,
                     CASE WHEN {_ENTRADA_DIA} <= p.dia_solicitacao THEN {_ENTRADA} END DESC NULLS LAST,
                     {_ENTRADA}, til.isn_internacao_leito
        ),
        base AS (
            SELECT p.isn_especialidade, p.dia_solicitacao, p.dia_parecer, p.horas, ti.isn_tipo_atendimento,
                   m.dsc_clinica, m.dsc_enfermaria, m.dsc_leito
            FROM periodo p
            JOIN t_internacao ti ON ti.isn_internacao = p.isn_internacao
            LEFT JOIN mov m ON m.id = p.id
        )
"""

# nome da especialidade (alias esp de t_especialidade, b da base): códigos de
# mesmo nome ficam juntos
_NOME_ESPECIALIDADE_SQL = f"""COALESCE(NULLIF(TRIM(esp.dsc_especialidade), ''),
                            CASE WHEN b.isn_especialidade IS NULL THEN '{SEM_ESPECIALIDADE}'
                                 ELSE 'Especialidade ' || b.isn_especialidade::text END)"""


def _filtrado_sql(filt_sql):
    """_BASE_SQL mais a CTE filtrado: a base com os filtros da página e o nome
    da especialidade (parâmetros: os de _BASE_SQL e depois os dos filtros)."""
    return _BASE_SQL.rstrip() + f""",
        filtrado AS (
            SELECT b.*, {_NOME_ESPECIALIDADE_SQL} AS especialidade
            FROM base b
            LEFT JOIN t_especialidade esp ON esp.isn_especialidade = b.isn_especialidade
            {filt_sql}
        )
"""


def _contagens(data_ini, data_fim, clinica, enfermaria, leito):
    """Solicitadas e pareceres do período por especialidade, tipo de
    atendimento e clínica (uma linha por combinação)."""
    filt_sql, filt_vals = _filtros_sql(clinica, enfermaria, leito, prefixo="WHERE")
    return fetch_all(_BASE_SQL + f"""
        SELECT b.isn_especialidade, esp.dsc_especialidade, b.isn_tipo_atendimento, b.dsc_clinica,
               COUNT(*) FILTER (WHERE b.dia_solicitacao BETWEEN %s AND %s) AS solicitadas,
               COUNT(*) FILTER (WHERE b.dia_parecer BETWEEN %s AND %s) AS pareceres
        FROM (SELECT * FROM base {filt_sql}) b
        LEFT JOIN t_especialidade esp ON esp.isn_especialidade = b.isn_especialidade
        GROUP BY 1, 2, 3, 4
    """, [data_ini, data_fim] * 4 + filt_vals)  # na ordem do texto: período, contagens, filtros


def _tempos(data_ini, data_fim, clinica, enfermaria, leito):
    """Horas entre a solicitação e o parecer dos pareceres do período, por
    especialidade (pelo nome: códigos de mesmo nome juntos) e no total
    (linha com especialidade NULL). Parecer registrado antes da solicitação
    (horas < 0) fica de fora e é contado em invalidos."""
    filt_sql, filt_vals = _filtros_sql(clinica, enfermaria, leito, prefixo="WHERE")
    return fetch_all(_BASE_SQL + f"""
        SELECT especialidade, GROUPING(especialidade) = 1 AS total,
               COUNT(*) FILTER (WHERE horas >= 0) AS pareceres,
               COUNT(*) FILTER (WHERE horas < 0) AS invalidos,
               AVG(horas) FILTER (WHERE horas >= 0) AS media,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY horas) FILTER (WHERE horas >= 0) AS mediana
        FROM (
            SELECT {_NOME_ESPECIALIDADE_SQL} AS especialidade, b.horas
            FROM (SELECT * FROM base {filt_sql}) b
            LEFT JOIN t_especialidade esp ON esp.isn_especialidade = b.isn_especialidade
            WHERE b.dia_parecer BETWEEN %s AND %s
        ) t
        GROUP BY GROUPING SETS ((especialidade), ())
    """, [data_ini, data_fim] * 2 + filt_vals + [data_ini, data_fim])


def _ordenar(itens):
    """Do maior número de solicitadas para o menor (desempate: pareceres, nome)."""
    return sorted(itens, key=lambda x: (-x['solicitadas'], -x['pareceres'], x['nome']))


def _tempo_parecer(data_ini, data_fim, clinica, enfermaria, leito):
    """Tempo entre a solicitação e o parecer (horas, 1 casa): por especialidade,
    da que tem mais pareceres para a que tem menos, e o total."""
    def horas(v):
        return round(float(v), 1) if v is not None else None
    linhas, total = [], None
    for r in _tempos(data_ini, data_fim, clinica, enfermaria, leito) or []:
        item = {'nome': r['especialidade'], 'pareceres': int(r['pareceres']), 'invalidos': int(r['invalidos']),
                'media': horas(r['media']), 'mediana': horas(r['mediana'])}
        if r['total']:
            total = item
        elif item['pareceres'] or item['invalidos']:
            linhas.append(item)
    linhas.sort(key=lambda x: (-x['pareceres'], x['nome']))
    return {'linhas': linhas,
            'total': total or {'nome': 'Total', 'pareceres': 0, 'invalidos': 0, 'media': None, 'mediana': None}}


def get_interconsultas(data_ini, data_fim, clinica=None, enfermaria=None, leito=None):
    """Cards (solicitadas e pareceres: total, emergência e internação) e as
    tabelas por especialidade e por especialidade e clínica."""
    faltam = colunas_faltando()
    if faltam:
        raise ValueError('A tabela t_interconsulta não tem a(s) coluna(s): ' + ', '.join(faltam) + '.')
    cards = {f'{medida}_{tipo}': 0 for medida in ('solicitadas', 'pareceres')
             for tipo in ('total', 'emergencia', 'internacao')}
    por_esp = {}
    for r in _contagens(data_ini, data_fim, clinica, enfermaria, leito) or []:
        sol, par = int(r['solicitadas']), int(r['pareceres'])
        if not sol and not par:
            continue
        tipo = {TIPO_EMERGENCIA: 'emergencia', TIPO_INTERNACAO: 'internacao'}.get(
            int(r['isn_tipo_atendimento']) if r['isn_tipo_atendimento'] is not None else None)
        for medida, qtd in (('solicitadas', sol), ('pareceres', par)):
            cards[f'{medida}_total'] += qtd
            if tipo:
                cards[f'{medida}_{tipo}'] += qtd
        nome_esp = (r['dsc_especialidade'] or '').strip() or (
            SEM_ESPECIALIDADE if r['isn_especialidade'] is None else f"Especialidade {int(r['isn_especialidade'])}")
        esp = por_esp.setdefault(nome_esp, {'nome': nome_esp, 'solicitadas': 0, 'pareceres': 0, 'clinicas': {}})
        esp['solicitadas'] += sol
        esp['pareceres'] += par
        nome_cli = (r['dsc_clinica'] or '').strip() or SEM_CLINICA
        cli = esp['clinicas'].setdefault(nome_cli, {'nome': nome_cli, 'solicitadas': 0, 'pareceres': 0})
        cli['solicitadas'] += sol
        cli['pareceres'] += par

    especialidades = _ordenar(por_esp.values())
    return {
        'cards': cards,
        'por_especialidade': [{k: e[k] for k in ('nome', 'solicitadas', 'pareceres')} for e in especialidades],
        # cada especialidade com as clínicas que solicitaram (da maior para a menor)
        'por_especialidade_clinica': [
            {'nome': e['nome'], 'solicitadas': e['solicitadas'], 'pareceres': e['pareceres'],
             'clinicas': _ordenar(e['clinicas'].values())}
            for e in especialidades
        ],
        'tempo_parecer': _tempo_parecer(data_ini, data_fim, clinica, enfermaria, leito),
    }


# ---------------------------------------------------------------------------
# Tendência mensal e comparativo
# (mesmo formato de get_mensal_hospitalar: "meses", "meses_iso" e uma série
# por indicador, um valor por mês; com por_ano, um valor por ano, para o
# comparativo anual)
# ---------------------------------------------------------------------------

# linhas do comparativo (chave, rótulo, unidade[, grupo, detalhe])
CAMPOS_MENSAL = [
    ('solicitadas_total', 'Interconsultas Solicitadas', 'qtd'),
    ('solicitadas_emergencia', 'Na emergência', 'qtd', None, True),
    ('solicitadas_internacao', 'Na internação', 'qtd', None, True),
    ('pareceres_total', 'Pareceres', 'qtd'),
    ('pareceres_emergencia', 'Na emergência', 'qtd', None, True),
    ('pareceres_internacao', 'Na internação', 'qtd', None, True),
    ('tempo_medio', 'Tempo Médio até o Parecer', 'h'),
    ('tempo_mediano', 'Tempo Mediano até o Parecer', 'h'),
]
# gráficos por especialidade: as maiores e, juntas, as demais
ESPECIALIDADES_NO_GRAFICO = 8


def _mensal_contagens(dt_ini, dt_fim, unidade, clinica, enfermaria, leito):
    """Solicitadas (mês da solicitação) e pareceres (mês do parecer) por
    mês/ano, tipo de atendimento e especialidade."""
    filt_sql, filt_vals = _filtros_sql(clinica, enfermaria, leito, prefixo="WHERE")
    return fetch_all(_filtrado_sql(filt_sql) + f"""
        SELECT 'solicitadas' AS medida, DATE_TRUNC('{unidade}', dia_solicitacao)::date AS mes,
               isn_tipo_atendimento, especialidade, COUNT(*) AS qtd
        FROM filtrado WHERE dia_solicitacao BETWEEN %s AND %s
        GROUP BY 2, 3, 4
        UNION ALL
        SELECT 'pareceres', DATE_TRUNC('{unidade}', dia_parecer)::date, isn_tipo_atendimento, especialidade, COUNT(*)
        FROM filtrado WHERE dia_parecer BETWEEN %s AND %s
        GROUP BY 2, 3, 4
    """, [dt_ini, dt_fim] * 2 + filt_vals + [dt_ini, dt_fim] * 2)


def _mensal_tempos(dt_ini, dt_fim, unidade, clinica, enfermaria, leito):
    """Horas até o parecer (média e mediana) por mês/ano do parecer; parecer
    registrado antes da solicitação fica de fora."""
    filt_sql, filt_vals = _filtros_sql(clinica, enfermaria, leito, prefixo="WHERE")
    return fetch_all(_filtrado_sql(filt_sql) + f"""
        SELECT DATE_TRUNC('{unidade}', dia_parecer)::date AS mes,
               AVG(horas) AS media, percentile_cont(0.5) WITHIN GROUP (ORDER BY horas) AS mediana
        FROM filtrado WHERE dia_parecer BETWEEN %s AND %s AND horas >= 0
        GROUP BY 1
    """, [dt_ini, dt_fim] * 2 + filt_vals + [dt_ini, dt_fim])


def _series_especialidade(por_esp, n):
    """[{nome, valores}] das ESPECIALIDADES_NO_GRAFICO maiores (do maior total
    para o menor) e, numa série só, as demais."""
    ordem = sorted((e for e in por_esp.items() if sum(e[1])), key=lambda e: (-sum(e[1]), e[0]))
    series = [{'nome': nome, 'valores': valores} for nome, valores in ordem[:ESPECIALIDADES_NO_GRAFICO]]
    demais = ordem[ESPECIALIDADES_NO_GRAFICO:]
    if demais:
        series.append({'nome': f'Demais especialidades ({len(demais)})',
                       'valores': [sum(v[i] for _, v in demais) for i in range(n)]})
    return series


def get_mensal(mes_ini, mes_fim, clinica=None, enfermaria=None, leito=None, por_ano=False):
    """Indicadores das interconsultas mês a mês (tendência mensal e
    comparativo): solicitadas e pareceres (total, emergência e internação),
    tempo médio e mediano até o parecer e as solicitadas e os pareceres por
    especialidade (gráficos da tendência)."""
    faltam = colunas_faltando()
    if faltam:
        raise ValueError('A tabela t_interconsulta não tem a(s) coluna(s): ' + ', '.join(faltam) + '.')
    dt_ini, dt_fim = _resolve_month_window(mes_ini, mes_fim)
    chaves = []
    m = date(dt_ini.year, 1 if por_ano else dt_ini.month, 1)
    while m <= dt_fim:
        chaves.append(m)
        m = _add_months(m, 12 if por_ano else 1)
    idx = {c: i for i, c in enumerate(chaves)}
    n = len(chaves)
    unidade = 'year' if por_ano else 'month'

    dados = {f'{medida}_{tipo}': [0] * n for medida in ('solicitadas', 'pareceres')
             for tipo in ('total', 'emergencia', 'internacao')}
    por_esp = {'solicitadas': {}, 'pareceres': {}}
    for r in _mensal_contagens(dt_ini, dt_fim, unidade, clinica, enfermaria, leito) or []:
        i = idx.get(r['mes'])
        if i is None:
            continue
        qtd, medida = int(r['qtd']), r['medida']
        tipo = {TIPO_EMERGENCIA: 'emergencia', TIPO_INTERNACAO: 'internacao'}.get(
            int(r['isn_tipo_atendimento']) if r['isn_tipo_atendimento'] is not None else None)
        dados[f'{medida}_total'][i] += qtd
        if tipo:
            dados[f'{medida}_{tipo}'][i] += qtd
        por_esp[medida].setdefault(r['especialidade'], [0] * n)[i] += qtd

    dados['tempo_medio'], dados['tempo_mediano'] = [None] * n, [None] * n
    for r in _mensal_tempos(dt_ini, dt_fim, unidade, clinica, enfermaria, leito) or []:
        i = idx.get(r['mes'])
        if i is not None:
            dados['tempo_medio'][i] = round(float(r['media']), 1)
            dados['tempo_mediano'][i] = round(float(r['mediana']), 1)

    dados.update({
        'meses': _rotulos(chaves, por_ano),
        'meses_iso': _meses_iso(chaves),
        'solicitadas_especialidade': _series_especialidade(por_esp['solicitadas'], n),
        'pareceres_especialidade': _series_especialidade(por_esp['pareceres'], n),
    })
    return dados
