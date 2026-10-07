"""
indicadores_hcasg.py

Módulo de consultas para as páginas de Indicadores Hospitalares e
Indicadores da Emergência deste painel.

Motivação: a página única de tendências mensais (no painel de indicadores
LEAN) calculava TODOS os indicadores (hospitalares + emergência) em uma
única chamada, com várias CTEs pesadas. Aqui a mesma lógica de negócio é
mantida, mas dividida em duas funções independentes:

  - get_mensal_hospitalar(...)  -> paciente-dia, saídas, TMP Hospitalar,
                                    giro de leito, leito-dia, taxa de
                                    ocupação.
  - get_mensal_emergencia(...)  -> LOS sem/com internação, TMP leitos PS,
                                    saídas PS, giro de leito PS, tempo
                                    porta-médico, internações PS.

Cada função só executa as consultas que o respectivo indicador precisa,
então a página "Indicadores Hospitalares" não paga o custo das consultas
de emergência, e vice-versa.

A página "Indicadores LEAN" (/lean), que antes era um app separado, usa
este mesmo módulo: get_indicadores / get_indicadores_mensais apenas juntam
os dois blocos acima, e as funções get_auditoria_* listam os registros que
compõem cada indicador. Assim todas as páginas mostram os mesmos números.
"""

import statistics
import time
from datetime import date, timedelta

import psycopg2
import psycopg2.extras
import psycopg2.errorcodes

import config_banco

# ---------------------------------------------------------------------------
# Conexão: dados de acesso e schema vêm de config_banco.py (página
# /admin/banco, variáveis DB_* ou padrões). As consultas não fixam schema;
# vale o search_path da conexão.
# ---------------------------------------------------------------------------

_RETRY_ERRORS = {
    psycopg2.errorcodes.ADMIN_SHUTDOWN,
    psycopg2.errorcodes.CRASH_SHUTDOWN,
    psycopg2.errorcodes.CANNOT_CONNECT_NOW,
    psycopg2.errorcodes.CONNECTION_FAILURE,
    psycopg2.errorcodes.SQLCLIENT_UNABLE_TO_ESTABLISH_SQLCONNECTION,
}
_MAX_RETRIES = 4
_RETRY_DELAY = 2


def get_conn():
    return config_banco.conectar()


def _should_retry(exc):
    # Falhas de conexão (servidor reiniciando, recusando conexões, conexão
    # caída no meio da consulta) chegam como OperationalError sem pgcode.
    if isinstance(exc, psycopg2.OperationalError) and not exc.pgcode:
        return True
    return exc.pgcode in _RETRY_ERRORS or "conflict with recovery" in str(exc).lower()


def _executar(sql, params, fetch):
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        conn = None
        try:
            conn = get_conn()
            # `with conn` só faz commit/rollback; o close() fica no finally.
            with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params or [])
                return fetch(cur)
        except psycopg2.Error as e:
            last_exc = e
            if _should_retry(e) and attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAY)
                continue
            raise
        finally:
            if conn is not None:
                conn.close()
    raise last_exc


def fetch_one(sql, params=None):
    return _executar(sql, params, lambda cur: cur.fetchone())


def fetch_all(sql, params=None):
    return _executar(sql, params, lambda cur: cur.fetchall())


# ---------------------------------------------------------------------------
# Constantes de clínicas (idênticas às do painel de indicadores LEAN)
# ---------------------------------------------------------------------------

CLINICAS_LOS_COM = (1, 15, 17, 18, 20, 21, 22, 23, 24, 34, 35, 36, 37, 38, 42, 50, 52)
CLINICAS_TMPH = (15, 17, 18, 20, 21, 22, 23, 24, 34, 35, 36, 37, 38, 42, 44, 46, 50, 52, 53, 54)
CLINICAS_PS = (15, 17, 18, 20, 21, 22, 23, 24, 34, 36, 37, 38, 42, 50, 52, 54)
CLINICAS_TMP_PS = tuple(v for v in CLINICAS_TMPH if v != 35)
LEITOS_TMP_PS_EXCL = (227, 228, 229, 230, 231, 232, 262, 263, 264, 265, 278, 1179, 279, 1180, 466, 467, 468, 469)
CLINICAS_INTERNACOES = tuple(sorted(set(CLINICAS_TMPH) | {26, 27, 33, 48}))
CLINICAS_FILTRO = tuple(sorted(set(CLINICAS_LOS_COM) | set(CLINICAS_TMPH) | set(CLINICAS_INTERNACOES)))

CLINICAS_LOS_COM_SQL = ", ".join(str(v) for v in CLINICAS_LOS_COM)
CLINICAS_LOS_COM_DEST_SQL = ", ".join(str(v) for v in CLINICAS_LOS_COM if v != 1)
CLINICAS_TMPH_SQL = ", ".join(str(v) for v in CLINICAS_TMPH)
CLINICAS_TMP_PS_SQL = ", ".join(str(v) for v in CLINICAS_TMP_PS)
LEITOS_TMP_PS_EXCL_SQL = ", ".join(str(v) for v in LEITOS_TMP_PS_EXCL)
CLINICAS_FILTRO_SQL = ", ".join(str(v) for v in CLINICAS_FILTRO)
CLINICAS_INTERNACOES_SQL = ", ".join(str(v) for v in CLINICAS_INTERNACOES)
CLINICAS_PS_SQL = ", ".join(str(v) for v in CLINICAS_PS)


def _add_months(dt, months):
    year = dt.year + (dt.month - 1 + months) // 12
    month = (dt.month - 1 + months) % 12 + 1
    return dt.replace(year=year, month=month, day=1)


def _resolve_month_window(mes_ini, mes_fim):
    today_month = date.today().replace(day=1)
    default_ini = _add_months(today_month, -12)
    default_fim = _add_months(today_month, -1)

    try:
        ini = date.fromisoformat(f"{mes_ini}-01") if mes_ini else default_ini
    except Exception:
        ini = default_ini
    try:
        fim = date.fromisoformat(f"{mes_fim}-01") if mes_fim else default_fim
    except Exception:
        fim = default_fim

    if ini > fim:
        ini, fim = fim, ini

    dt_ini = ini
    dt_fim = _add_months(fim, 1) - date.resolution
    return dt_ini, dt_fim


def get_filtros():
    clinicas = fetch_all(f"""
        SELECT DISTINCT tc.dsc_clinica
        FROM t_internacao_leito til
        JOIN t_clinica tc ON tc.isn_clinica = til.isn_clinica
        WHERE til.isn_clinica IN ({CLINICAS_FILTRO_SQL})
        ORDER BY 1
    """)
    enfermar = fetch_all(f"""
        SELECT DISTINCT te.dsc_enfermaria
        FROM t_internacao_leito til
        JOIN t_enfermaria te ON te.isn_enfermaria = til.isn_enfermaria
        WHERE til.isn_clinica IN ({CLINICAS_FILTRO_SQL})
        ORDER BY 1
    """)
    leitos = fetch_all(f"""
        SELECT DISTINCT tl.dsc_leito
        FROM t_internacao_leito til
        JOIN t_leito tl ON tl.isn_leito = til.isn_leito
        WHERE til.isn_clinica IN ({CLINICAS_FILTRO_SQL})
        ORDER BY 1
    """)
    return (
        [r["dsc_clinica"] for r in clinicas],
        [r["dsc_enfermaria"] for r in enfermar],
        [r["dsc_leito"] for r in leitos],
    )


def _filtros_sql(clinica, enfermaria, leito, prefixo="AND"):
    conds, vals = [], []
    if clinica:
        if isinstance(clinica, list) and len(clinica) > 1:
            placeholders = ", ".join(["%s"] * len(clinica))
            conds.append(f"dsc_clinica IN ({placeholders})")
            vals.extend(clinica)
        else:
            c = clinica[0] if isinstance(clinica, list) else clinica
            conds.append("dsc_clinica = %s")
            vals.append(c)
    if enfermaria:
        conds.append("dsc_enfermaria = %s"); vals.append(enfermaria)
    if leito:
        conds.append("dsc_leito = %s"); vals.append(leito)
    if not conds:
        return "", []
    return prefixo + " " + " AND ".join(conds), vals


# Primeira movimentação de cada internação (clínica/enfermaria/leito de entrada).
_PRIMEIRA_CLINICA_CTE = """
    WITH primeira_clinica AS (
        SELECT DISTINCT ON (til.isn_internacao)
            til.isn_internacao,
            til.isn_clinica,
            tc.dsc_clinica,
            te.dsc_enfermaria,
            tl.dsc_leito
        FROM t_internacao_leito til
        JOIN t_clinica    tc ON tc.isn_clinica    = til.isn_clinica
        JOIN t_enfermaria te ON te.isn_enfermaria = til.isn_enfermaria
        JOIN t_leito      tl ON tl.isn_leito      = til.isn_leito
        ORDER BY til.isn_internacao,
                 COALESCE(til.dat_internacao_info, til.dat_internacao),
                 til.isn_internacao_leito
    )
"""


def _meses_labels(meses_dates):
    return [d.strftime("%b/%y") if d else None for d in meses_dates]


def _meses_iso(meses_dates):
    return [d.strftime("%Y-%m") if d else None for d in meses_dates]


# ---------------------------------------------------------------------------
# INDICADORES HOSPITALARES (mensal)
# paciente-dia, saídas, TMP Hospitalar, giro de leito, leito-dia,
# taxa de ocupação.
# ---------------------------------------------------------------------------

def get_mensal_hospitalar(clinica, enfermaria, leito, mes_ini=None, mes_fim=None):
    dt_ini, dt_fim = _resolve_month_window(mes_ini, mes_fim)
    filt_sql, filt_vals = _filtros_sql(clinica, enfermaria, leito, prefixo="AND")
    _clinica_unica = (isinstance(clinica, str) and clinica) or (isinstance(clinica, list) and len(clinica) == 1)
    incluir_destino_5 = "TRUE" if _clinica_unica else "FALSE"

    paciente_dia_expr = """
                    CASE
                        WHEN fim > ini THEN (fim - ini)::numeric
                        WHEN fim = ini AND dt_entrada = dt_saida AND COALESCE(isn_destino, 0) <> 1 THEN 1::numeric
                        ELSE 0::numeric
                    END
    """
    saida_valida_expr = f"""
                (
                    isn_destino IN (1, 3, 4)
                    OR (
                        {incluir_destino_5}
                        AND isn_destino = 5
                        AND (
                            isn_clinica <> 22
                            OR prox_clinica IN (40, 48, 15, 24)
                        )
                    )
                )
    """

    meses = fetch_all("""
        SELECT gs::date AS mes
        FROM generate_series(
            DATE_TRUNC('month', %s::date)::date,
            DATE_TRUNC('month', %s::date)::date,
            INTERVAL '1 month'
        ) AS gs
        ORDER BY mes
    """, [dt_ini, dt_fim])
    meses_keys = [row["mes"] for row in (meses or [])]

    rows_tmph = fetch_all(f"""
        WITH limites AS (
            SELECT %s::date AS dt_ini, %s::date AS dt_fim
        ),
        movimentos AS (
            SELECT
                til.*,
                LEAD(til.isn_clinica) OVER (
                    PARTITION BY til.isn_internacao
                    ORDER BY COALESCE(til.dat_internacao_info, til.dat_internacao), til.isn_internacao_leito
                ) AS prox_clinica
            FROM t_internacao_leito til
        ),
        base AS (
            SELECT
                mov.isn_internacao_leito AS id_mov,
                mov.isn_clinica,
                tc.dsc_clinica,
                te.dsc_enfermaria,
                tl.dsc_leito,
                mov.isn_destino,
                mov.prox_clinica,
                COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date AS dt_entrada,
                COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date AS dt_saida,
                mov.dat_alta::date AS dt_alta
            FROM movimentos mov
            JOIN t_clinica tc ON mov.isn_clinica = tc.isn_clinica
            JOIN t_enfermaria te ON mov.isn_enfermaria = te.isn_enfermaria
            JOIN t_leito tl ON mov.isn_leito = tl.isn_leito
            LEFT JOIN t_destino td ON mov.isn_destino = td.isn_destino
            WHERE mov.isn_clinica IN ({CLINICAS_TMPH_SQL})
              AND COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date <= (SELECT dt_fim FROM limites)
              AND COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date >= (SELECT dt_ini FROM limites)
              {filt_sql}
        ),
        partes AS (
            SELECT
                b.id_mov,
                gs.mes::date AS mes,
                b.isn_destino,
                b.dt_entrada,
                b.dt_saida,
                GREATEST(b.dt_entrada, gs.mes::date, (SELECT dt_ini FROM limites)) AS ini,
                LEAST(b.dt_saida, (gs.mes + INTERVAL '1 month')::date, (SELECT dt_fim FROM limites) + 1) AS fim  -- fim exclusivo: a noite da virada do mês conta no mês em que começa
            FROM base b
            CROSS JOIN LATERAL generate_series(
                DATE_TRUNC('month', GREATEST(b.dt_entrada, (SELECT dt_ini FROM limites)))::date,
                DATE_TRUNC('month', LEAST(b.dt_saida, (SELECT dt_fim FROM limites)))::date,
                INTERVAL '1 month'
            ) AS gs(mes)
        ),
        paciente_dia_mes AS (
            SELECT
                mes,
                SUM(
{paciente_dia_expr}
                )::numeric AS paciente_dia
            FROM partes
            GROUP BY mes
        ),
        saidas_mes AS (
            SELECT
                DATE_TRUNC('month', dt_alta)::date AS mes,
                SUM(CASE WHEN {saida_valida_expr} THEN 1 ELSE 0 END)::numeric AS saidas
            FROM base
            WHERE dt_alta BETWEEN (SELECT dt_ini FROM limites) AND (SELECT dt_fim FROM limites)
            GROUP BY DATE_TRUNC('month', dt_alta)::date
        )
        SELECT
            pd.mes,
            pd.paciente_dia,
            sm.saidas,
            ROUND(pd.paciente_dia / NULLIF(sm.saidas, 0), 2) AS tmph
        FROM paciente_dia_mes pd
        LEFT JOIN saidas_mes sm ON sm.mes = pd.mes
        ORDER BY pd.mes
    """, [dt_ini, dt_fim] + filt_vals)
    mapa_tmph = {r["mes"]: r["tmph"] for r in (rows_tmph or [])}
    mapa_paciente_dia = {r["mes"]: r["paciente_dia"] for r in (rows_tmph or [])}
    mapa_saidas = {r["mes"]: r["saidas"] for r in (rows_tmph or [])}

    rows_saidas_destino_mes = fetch_all(f"""
        WITH limites AS (
            SELECT %s::date AS dt_ini, %s::date AS dt_fim
        ),
        movimentos AS (
            SELECT
                til.*,
                LEAD(til.isn_clinica) OVER (
                    PARTITION BY til.isn_internacao
                    ORDER BY COALESCE(til.dat_internacao_info, til.dat_internacao), til.isn_internacao_leito
                ) AS prox_clinica
            FROM t_internacao_leito til
        ),
        base AS (
            SELECT
                mov.isn_clinica,
                tc.dsc_clinica,
                te.dsc_enfermaria,
                tl.dsc_leito,
                mov.isn_destino,
                td.dsc_destino,
                mov.prox_clinica,
                COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date AS dt_entrada,
                COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date AS dt_saida,
                mov.dat_alta::date AS dt_alta
            FROM movimentos mov
            JOIN t_clinica tc ON mov.isn_clinica = tc.isn_clinica
            JOIN t_enfermaria te ON mov.isn_enfermaria = te.isn_enfermaria
            JOIN t_leito tl ON mov.isn_leito = tl.isn_leito
            LEFT JOIN t_destino td ON mov.isn_destino = td.isn_destino
            WHERE mov.isn_clinica IN ({CLINICAS_TMPH_SQL})
              AND COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date <= (SELECT dt_fim FROM limites)
              AND COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date >= (SELECT dt_ini FROM limites)
              {filt_sql}
        )
        SELECT
            DATE_TRUNC('month', dt_alta)::date AS mes,
            isn_destino,
            COALESCE(dsc_destino, 'Destino ' || isn_destino::text) AS dsc_destino,
            COUNT(*)::numeric AS qtd
        FROM base
        WHERE {saida_valida_expr}
          AND isn_destino <> 4
          AND dt_alta BETWEEN (SELECT dt_ini FROM limites) AND (SELECT dt_fim FROM limites)
        GROUP BY DATE_TRUNC('month', dt_alta)::date, isn_destino, dsc_destino
        ORDER BY mes, isn_destino
    """, [dt_ini, dt_fim] + filt_vals)

    _leitos_filt_sql, _leitos_filt_vals = _filtros_sql(clinica, None, None, prefixo="AND")

    rows_leitos_internacao_mensais = fetch_all(f"""
        WITH
        limites AS (SELECT %s::date AS dt_ini, %s::date AS dt_fim),
        meses AS (
            SELECT generate_series(
                DATE_TRUNC('month', (SELECT dt_ini FROM limites))::date,
                DATE_TRUNC('month', (SELECT dt_fim FROM limites))::date,
                INTERVAL '1 month'
            )::date AS mes
        ),
        dias_por_mes AS (
            SELECT
                m.mes,
                generate_series(
                    GREATEST(m.mes, (SELECT dt_ini FROM limites)),
                    LEAST((m.mes + INTERVAL '1 month' - INTERVAL '1 day')::date, (SELECT dt_fim FROM limites)),
                    INTERVAL '1 day'
                )::date AS dt
            FROM meses m
        ),
        leitos_base_det AS (
            SELECT tl.isn_leito
            FROM t_leito tl
            JOIN t_enfermaria te ON te.isn_enfermaria = tl.isn_enfermaria
            JOIN t_clinica tc ON tc.isn_clinica = te.isn_clinica
            WHERE tl.flg_ativo = 'S'
              AND tl.flg_extra = 'N'
              AND te.flg_ativo = 'S'
              AND tc.flg_ativo = 'S'
              AND te.isn_clinica IN ({CLINICAS_INTERNACOES_SQL})
              {_leitos_filt_sql}
        ),
        leitos_base AS (
            SELECT COUNT(*) AS total FROM leitos_base_det
        ),
        interdicoes_por_dia AS (
            SELECT
                dpm.dt,
                COUNT(DISTINCT lbd.isn_leito) AS interditados
            FROM dias_por_mes dpm
            CROSS JOIN leitos_base_det lbd
            WHERE EXISTS (
                SELECT 1 FROM t_leito_interditado tli
                WHERE tli.isn_leito = lbd.isn_leito
                  AND tli.dat_interdicao::date <= dpm.dt
                  AND (tli.dat_liberacao IS NULL OR tli.dat_liberacao::date > dpm.dt)
                  AND EXTRACT(EPOCH FROM (COALESCE(tli.dat_liberacao, CURRENT_TIMESTAMP) - tli.dat_interdicao)) > 86400
            )
            GROUP BY dpm.dt
        )
        SELECT
            dpm.mes,
            ROUND(AVG(lb.total - COALESCE(ipd.interditados, 0))::numeric, 2) AS media_disponiveis
        FROM dias_por_mes dpm
        CROSS JOIN leitos_base lb
        LEFT JOIN interdicoes_por_dia ipd ON ipd.dt = dpm.dt
        GROUP BY dpm.mes, lb.total
        ORDER BY dpm.mes
    """, [dt_ini, dt_fim] + _leitos_filt_vals)
    mapa_leitos_internacao_mensais = {r["mes"]: r["media_disponiveis"] for r in (rows_leitos_internacao_mensais or [])}

    # Leito-dia: soma dia a dia dos leitos disponíveis (flg_ativo='S' e
    # flg_extra='N'), descontando apenas interdições com duração > 5 dias
    # (dat_interdicao / dat_liberacao por data; o dia da liberação já conta
    # como disponível).
    rows_leito_dia_mensal = fetch_all(f"""
        WITH
        limites AS (SELECT %s::date AS dt_ini, %s::date AS dt_fim),
        meses AS (
            SELECT generate_series(
                DATE_TRUNC('month', (SELECT dt_ini FROM limites))::date,
                DATE_TRUNC('month', (SELECT dt_fim FROM limites))::date,
                INTERVAL '1 month'
            )::date AS mes
        ),
        dias_por_mes AS (
            SELECT
                m.mes,
                generate_series(
                    GREATEST(m.mes, (SELECT dt_ini FROM limites)),
                    LEAST((m.mes + INTERVAL '1 month' - INTERVAL '1 day')::date, (SELECT dt_fim FROM limites)),
                    INTERVAL '1 day'
                )::date AS dt
            FROM meses m
        ),
        leitos_base_det AS (
            SELECT tl.isn_leito
            FROM t_leito tl
            JOIN t_enfermaria te ON te.isn_enfermaria = tl.isn_enfermaria
            JOIN t_clinica tc ON tc.isn_clinica = te.isn_clinica
            WHERE tl.flg_ativo = 'S'
              AND tl.flg_extra = 'N'
              AND te.flg_ativo = 'S'
              AND tc.flg_ativo = 'S'
              AND te.isn_clinica IN ({CLINICAS_TMPH_SQL})
              {filt_sql}
        ),
        leitos_base AS (
            SELECT COUNT(*) AS total FROM leitos_base_det
        ),
        interdicoes_por_dia AS (
            SELECT
                dpm.dt,
                COUNT(DISTINCT lbd.isn_leito) AS interditados
            FROM dias_por_mes dpm
            CROSS JOIN leitos_base_det lbd
            WHERE EXISTS (
                SELECT 1 FROM t_leito_interditado tli
                WHERE tli.isn_leito = lbd.isn_leito
                  AND tli.dat_interdicao::date <= dpm.dt
                  AND (tli.dat_liberacao IS NULL OR tli.dat_liberacao::date > dpm.dt)
                  AND (COALESCE(tli.dat_liberacao::date, CURRENT_DATE) - tli.dat_interdicao::date) > 5
            )
            GROUP BY dpm.dt
        )
        SELECT
            dpm.mes,
            SUM(lb.total - COALESCE(ipd.interditados, 0))::bigint AS leito_dia
        FROM dias_por_mes dpm
        CROSS JOIN leitos_base lb
        LEFT JOIN interdicoes_por_dia ipd ON ipd.dt = dpm.dt
        GROUP BY dpm.mes
        ORDER BY dpm.mes
    """, [dt_ini, dt_fim] + filt_vals)
    mapa_leito_dia = {r["mes"]: r["leito_dia"] for r in (rows_leito_dia_mensal or [])}

    # Óbitos por mês, classificados em:
    #   - Óbito Institucional: alta com destino 4 (óbito) e diferença entre a
    #     alta efetiva e o dat_cadastro da internação MAIOR que 24 horas.
    #   - Óbito Hospitalar: alta com destino 4 e diferença <= 24 horas.
    rows_obitos_mes = fetch_all(f"""
        WITH limites AS (SELECT %s::date AS dt_ini, %s::date AS dt_fim),
        movimentos AS (
            SELECT
                til.*,
                LEAD(til.isn_clinica) OVER (
                    PARTITION BY til.isn_internacao
                    ORDER BY COALESCE(til.dat_internacao_info, til.dat_internacao), til.isn_internacao_leito
                ) AS prox_clinica
            FROM t_internacao_leito til
        ),
        base AS (
            SELECT
                mov.isn_internacao,
                mov.isn_clinica,
                mov.isn_destino,
                mov.prox_clinica,
                COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date AS dt_entrada,
                COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date AS dt_saida,
                mov.dat_alta::date AS dt_alta,
                COALESCE(
                    mov.dat_alta_info + COALESCE(mov.hor_alta_info, '00:00')::time,
                    mov.dat_alta      + COALESCE(mov.hor_alta, '00:00')::time
                ) AS ts_alta
            FROM movimentos mov
            JOIN t_clinica tc ON mov.isn_clinica = tc.isn_clinica
            JOIN t_enfermaria te ON mov.isn_enfermaria = te.isn_enfermaria
            JOIN t_leito tl ON mov.isn_leito = tl.isn_leito
            WHERE mov.isn_clinica IN ({CLINICAS_TMPH_SQL})
              AND COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date <= (SELECT dt_fim FROM limites)
              AND COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date >= (SELECT dt_ini FROM limites)
              {filt_sql}
        ),
        base_ti AS (
            SELECT b.*, ti.dat_cadastro
            FROM base b
            JOIN t_internacao ti ON ti.isn_internacao = b.isn_internacao
        ),
        base_rotulo AS (
            SELECT
                *,
                CASE
                    WHEN isn_destino = 4
                         AND ts_alta IS NOT NULL AND dat_cadastro IS NOT NULL
                         AND EXTRACT(EPOCH FROM (ts_alta - dat_cadastro)) > 86400
                        THEN 'institucional'
                    WHEN isn_destino = 4
                        THEN 'hospitalar'
                    ELSE NULL
                END AS categoria_obito
            FROM base_ti
        )
        SELECT
            DATE_TRUNC('month', dt_alta)::date AS mes,
            categoria_obito,
            COUNT(*)::numeric AS qtd
        FROM base_rotulo
        WHERE {saida_valida_expr}
          AND dt_alta BETWEEN (SELECT dt_ini FROM limites) AND (SELECT dt_fim FROM limites)
          AND categoria_obito IS NOT NULL
        GROUP BY DATE_TRUNC('month', dt_alta)::date, categoria_obito
        ORDER BY mes
    """, [dt_ini, dt_fim] + filt_vals)
    mapa_obito_institucional = {}
    mapa_obito_hospitalar = {}
    for r in (rows_obitos_mes or []):
        if r["categoria_obito"] == "institucional":
            mapa_obito_institucional[r["mes"]] = r["qtd"]
        elif r["categoria_obito"] == "hospitalar":
            mapa_obito_hospitalar[r["mes"]] = r["qtd"]

    result = {
        "meses": _meses_labels(meses_keys),
        "meses_iso": _meses_iso(meses_keys),
        "paciente_dia": [],
        "saidas": [],
        "saidas_destinos_mensais": {},
        "tmph": [],
        "giro": [],
        "leito_dia": [],
        "taxa_ocupacao": [],
        "total_obitos": [],
        "obitos_institucionais": [],
        "obitos_hospitalares": [],
        "taxa_mortalidade_hospitalar": [],
        "taxa_mortalidade_institucional": [],
    }

    mapa_saidas_dest = {}
    for r in (rows_saidas_destino_mes or []):
        k = str(r["isn_destino"])
        if k not in mapa_saidas_dest:
            mapa_saidas_dest[k] = {
                "isn_destino": r["isn_destino"],
                "dsc_destino": r["dsc_destino"],
                "por_mes": {}
            }
        mapa_saidas_dest[k]["por_mes"][r["mes"]] = r["qtd"]

    # Óbitos entram no gráfico de Saídas já separados em Institucional e
    # Hospitalar (em vez de uma única categoria "Óbito").
    if mapa_obito_institucional:
        mapa_saidas_dest["4001"] = {
            "isn_destino": 4001,
            "dsc_destino": "Óbito Institucional",
            "por_mes": dict(mapa_obito_institucional),
        }
    if mapa_obito_hospitalar:
        mapa_saidas_dest["4002"] = {
            "isn_destino": 4002,
            "dsc_destino": "Óbito Hospitalar",
            "por_mes": dict(mapa_obito_hospitalar),
        }

    for mes in meses_keys:
        saidas_mes = mapa_saidas.get(mes)
        leitos_internacao_mes = mapa_leitos_internacao_mensais.get(mes)
        giro_mes = (
            round(float(saidas_mes) / float(leitos_internacao_mes), 2)
            if saidas_mes is not None and leitos_internacao_mes else None
        )
        pac_dia_mes = mapa_paciente_dia.get(mes)
        leito_dia_mes = mapa_leito_dia.get(mes)

        result["paciente_dia"].append(pac_dia_mes)
        result["saidas"].append(saidas_mes)
        result["tmph"].append(mapa_tmph.get(mes))
        result["giro"].append(giro_mes)
        result["leito_dia"].append(leito_dia_mes)
        result["taxa_ocupacao"].append(
            round(float(pac_dia_mes) / float(leito_dia_mes) * 100, 2)
            if pac_dia_mes is not None and leito_dia_mes else None
        )

        obito_inst_mes = mapa_obito_institucional.get(mes, 0) or 0
        obito_hosp_mes = mapa_obito_hospitalar.get(mes, 0) or 0
        total_obitos_mes = float(obito_inst_mes) + float(obito_hosp_mes)
        result["total_obitos"].append(total_obitos_mes)
        result["obitos_institucionais"].append(float(obito_inst_mes))
        result["obitos_hospitalares"].append(float(obito_hosp_mes))
        result["taxa_mortalidade_hospitalar"].append(
            round(total_obitos_mes / float(saidas_mes) * 100, 2)
            if saidas_mes else None
        )
        result["taxa_mortalidade_institucional"].append(
            round(float(obito_inst_mes) / float(saidas_mes) * 100, 2)
            if saidas_mes else None
        )

    for k, info in sorted(mapa_saidas_dest.items(), key=lambda x: int(x[0])):
        result["saidas_destinos_mensais"][k] = {
            "isn_destino": info["isn_destino"],
            "dsc_destino": info["dsc_destino"],
            "valores": [info["por_mes"].get(mes, 0) for mes in meses_keys]
        }

    return result


# ---------------------------------------------------------------------------
# INDICADORES DA EMERGÊNCIA (mensal)
# LOS sem/com internação, TMP leitos PS, saídas PS, giro de leito PS,
# tempo porta-médico, internações PS.
# ---------------------------------------------------------------------------

def get_mensal_emergencia(clinica, enfermaria, leito, mes_ini=None, mes_fim=None):
    dt_ini, dt_fim = _resolve_month_window(mes_ini, mes_fim)
    filt_sql, filt_vals = _filtros_sql(clinica, enfermaria, leito, prefixo="AND")
    _clinica_unica = (isinstance(clinica, str) and clinica) or (isinstance(clinica, list) and len(clinica) == 1)
    incluir_destino_5 = "TRUE" if _clinica_unica else "FALSE"

    paciente_dia_expr = """
                    CASE
                        WHEN fim > ini THEN (fim - ini)::numeric
                        WHEN fim = ini AND dt_entrada = dt_saida AND COALESCE(isn_destino, 0) <> 1 THEN 1::numeric
                        ELSE 0::numeric
                    END
    """
    saida_valida_expr = f"""
                (
                    isn_destino IN (1, 3, 4)
                    OR (
                        {incluir_destino_5}
                        AND isn_destino = 5
                        AND (
                            isn_clinica <> 22
                            OR prox_clinica IN (40, 48, 15, 24)
                        )
                    )
                )
    """

    meses = fetch_all("""
        SELECT gs::date AS mes
        FROM generate_series(
            DATE_TRUNC('month', %s::date)::date,
            DATE_TRUNC('month', %s::date)::date,
            INTERVAL '1 month'
        ) AS gs
        ORDER BY mes
    """, [dt_ini, dt_fim])
    meses_keys = [row["mes"] for row in (meses or [])]

    rows_sem = fetch_all("""
        SELECT
            DATE_TRUNC('month', ti.dat_cadastro)::date AS mes,
            ROUND(
                AVG(EXTRACT(EPOCH FROM (
                    COALESCE(til.dat_alta_info + til.hor_alta_info::time,
                             til.dat_alta      + til.hor_alta::time)
                    -
                    COALESCE(til.dat_internacao_info + til.hor_internacao_info::time,
                             til.dat_internacao      + til.hor_internacao::time)
                )) / 60)::numeric, 2
            ) AS los_sem
        FROM t_internacao ti
        LEFT JOIN t_tipo_atendimento tta ON ti.isn_tipo_atendimento = tta.isn_tipo_atendimento
        LEFT JOIN t_internacao_leito til ON ti.isn_internacao = til.isn_internacao
        JOIN  t_paciente tp ON ti.isn_paciente = tp.isn_paciente
        JOIN  t_destino  td ON til.isn_destino = td.isn_destino
        WHERE ti.isn_tipo_atendimento = 2
          AND til.isn_destino IN (1, 3, 4)
          AND til.dat_alta IS NOT NULL
          AND ti.dat_cadastro::date BETWEEN %s AND %s
        GROUP BY DATE_TRUNC('month', ti.dat_cadastro)::date
        ORDER BY mes
        """, [dt_ini, dt_fim])
    mapa_los_sem = {r["mes"]: r["los_sem"] for r in (rows_sem or [])}

    rows_com = fetch_all(f"""
        WITH
        internacao_datas AS (
            SELECT
                ti.isn_internacao,
                tp.num_prontuario,
                MIN(til.dat_internacao) AS dat_entrada,
                MAX(til.dat_alta)       AS dat_saida
            FROM t_internacao ti
            JOIN t_paciente tp ON tp.isn_paciente = ti.isn_paciente
            JOIN t_internacao_leito til ON til.isn_internacao = ti.isn_internacao
            WHERE til.dat_internacao IS NOT NULL
            GROUP BY ti.isn_internacao, tp.num_prontuario
        ),
        continuidade AS (
            SELECT
                isn_internacao,
                num_prontuario,
                dat_entrada,
                dat_saida,
                CASE
                    WHEN LAG(dat_saida) OVER (
                        PARTITION BY num_prontuario ORDER BY dat_entrada, isn_internacao
                    ) = dat_entrada
                    THEN 0 ELSE 1
                END AS novo_grupo
            FROM internacao_datas
        ),
        grupos AS (
            SELECT
                isn_internacao,
                num_prontuario,
                dat_entrada,
                SUM(novo_grupo) OVER (
                    PARTITION BY num_prontuario ORDER BY dat_entrada, isn_internacao
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS grp_num
            FROM continuidade
        ),
        cadeia AS (
            SELECT
                g.isn_internacao,
                FIRST_VALUE(g.isn_internacao) OVER (
                    PARTITION BY g.num_prontuario, g.grp_num
                    ORDER BY g.dat_entrada, g.isn_internacao
                ) AS id_grupo
            FROM grupos g
        ),
        base AS (
            SELECT
                c.id_grupo,
                til.isn_clinica,
                COALESCE(
                    til.dat_internacao_info + til.hor_internacao_info::time,
                    til.dat_internacao + til.hor_internacao::time
                ) AS ts_internacao,
                COALESCE(
                    til.dat_alta_info + til.hor_alta_info::time,
                    til.dat_alta + til.hor_alta::time
                ) AS ts_alta
            FROM cadeia c
            JOIN t_internacao_leito til ON til.isn_internacao = c.isn_internacao
            WHERE til.dat_internacao IS NOT NULL
        ),
        grupo_primeiro AS (
            SELECT
                b.id_grupo,
                MIN(b.ts_internacao) AS ts_primeiro_grupo
            FROM base b
            GROUP BY b.id_grupo
        ),
        grupo_inicio AS (
            SELECT
                b.id_grupo,
                MIN(b.ts_internacao) AS ts_clinica_1,
                gp.ts_primeiro_grupo
            FROM base b
            JOIN grupo_primeiro gp ON gp.id_grupo = b.id_grupo
            JOIN t_internacao ti ON ti.isn_internacao = b.id_grupo
            JOIN (
                SELECT DISTINCT ON (isn_internacao) isn_internacao, isn_destino
                FROM t_internacao_leito
                ORDER BY isn_internacao,
                    COALESCE(dat_alta_info + hor_alta_info::time, dat_alta + hor_alta::time) DESC NULLS LAST,
                    isn_internacao_leito DESC
            ) til_ult ON til_ult.isn_internacao = b.id_grupo
            WHERE b.isn_clinica = 1
              AND ti.isn_tipo_atendimento = 2
              AND til_ult.isn_destino = 13
              AND gp.ts_primeiro_grupo::date BETWEEN %s AND %s
            GROUP BY b.id_grupo, gp.ts_primeiro_grupo
        ),
        grupo_destino AS (
            SELECT
                b.id_grupo,
                MIN(b.ts_internacao) AS ts_primeira_clinica
            FROM base b
            JOIN grupo_inicio g ON g.id_grupo = b.id_grupo
            WHERE b.isn_clinica IN ({CLINICAS_LOS_COM_DEST_SQL})
              AND b.ts_internacao IS NOT NULL
              AND b.ts_internacao >= g.ts_clinica_1
            GROUP BY b.id_grupo
        ),
        grupo_fallback AS (
            SELECT
                b.id_grupo,
                MAX(COALESCE(b.ts_alta, CURRENT_TIMESTAMP)) AS ts_ultima_alta
            FROM base b
            JOIN grupo_inicio g ON g.id_grupo = b.id_grupo
            WHERE b.ts_internacao >= g.ts_clinica_1
            GROUP BY b.id_grupo
        ),
        grupo_final AS (
            SELECT
                g.id_grupo,
                g.ts_clinica_1,
                g.ts_primeiro_grupo,
                COALESCE(d.ts_primeira_clinica, f.ts_ultima_alta) AS ts_referencia
            FROM grupo_inicio g
            LEFT JOIN grupo_destino d ON d.id_grupo = g.id_grupo
            LEFT JOIN grupo_fallback f ON f.id_grupo = g.id_grupo
        ),
        los_por_grupo AS (
            SELECT
                gf.id_grupo,
                DATE_TRUNC('month', gf.ts_primeiro_grupo)::date AS mes,
                EXTRACT(EPOCH FROM (gf.ts_referencia - gf.ts_clinica_1)) / 60 AS los_min
            FROM grupo_final gf
            WHERE gf.ts_referencia > gf.ts_clinica_1
        )
        SELECT
            lpg.mes,
            ROUND(AVG(lpg.los_min)::numeric, 2) AS los_com
        FROM los_por_grupo lpg
        GROUP BY lpg.mes
        ORDER BY mes
    """, [dt_ini, dt_fim])
    mapa_los_com = {r["mes"]: r["los_com"] for r in (rows_com or [])}

    rows_porta_medico = fetch_all(f"""
        WITH limites AS (
            SELECT
                %s::date AS dt_ini,
                %s::date AS dt_fim
        ),
        primeira_consulta AS (
            SELECT
                tia.isn_internacao,
                MIN(
                    tia.dat_ini
                    + COALESCE(NULLIF(TRIM(tia.dsc_hor_ini), ''), '00:00')::time
                ) AS ts_consulta_med
            FROM t_internacao_atendimento tia
            WHERE tia.dat_ini IS NOT NULL
            GROUP BY tia.isn_internacao
        ),
        mov_inicio AS (
            SELECT DISTINCT ON (til.isn_internacao)
                til.isn_internacao,
                tc.dsc_clinica,
                te.dsc_enfermaria,
                tl.dsc_leito
            FROM t_internacao_leito til
            JOIN t_clinica tc ON til.isn_clinica = tc.isn_clinica
            JOIN t_enfermaria te ON til.isn_enfermaria = te.isn_enfermaria
            JOIN t_leito tl ON til.isn_leito = tl.isn_leito
            WHERE til.isn_clinica IN ({CLINICAS_FILTRO_SQL})
            ORDER BY
                til.isn_internacao,
                COALESCE(til.dat_internacao_info, til.dat_internacao),
                til.isn_internacao_leito
        )
        SELECT
            DATE_TRUNC('month', ti.dat_cadastro)::date AS mes,
            ROUND(
                AVG(EXTRACT(EPOCH FROM (pc.ts_consulta_med - ti.dat_cadastro)) / 60)::numeric,
                2
            ) AS porta_medico
        FROM t_internacao ti
        JOIN mov_inicio mi ON mi.isn_internacao = ti.isn_internacao
        JOIN primeira_consulta pc ON pc.isn_internacao = ti.isn_internacao
        LEFT JOIN t_admissao ta ON ta.isn_internacao = ti.isn_internacao
        WHERE ti.isn_tipo_atendimento = 2
          AND ti.dat_cadastro::date BETWEEN (SELECT dt_ini FROM limites) AND (SELECT dt_fim FROM limites)
          AND pc.ts_consulta_med >= ti.dat_cadastro
          {filt_sql}
        GROUP BY DATE_TRUNC('month', ti.dat_cadastro)::date
        ORDER BY mes
    """, [dt_ini, dt_fim] + filt_vals)
    mapa_porta_medico = {r["mes"]: r["porta_medico"] for r in (rows_porta_medico or [])}

    rows_tmp_ps = fetch_all(f"""
        WITH limites AS (
            SELECT
                %s::date AS dt_ini,
                %s::date AS dt_fim
        ),
        movimentos AS (
            SELECT
                til.*,
                LEAD(til.isn_clinica) OVER (
                    PARTITION BY til.isn_internacao
                    ORDER BY COALESCE(til.dat_internacao_info, til.dat_internacao), til.isn_internacao_leito
                ) AS prox_clinica
            FROM t_internacao_leito til
        ),
        base AS (
            SELECT
                mov.isn_internacao_leito AS id_mov,
                mov.isn_clinica,
                tc.dsc_clinica,
                te.dsc_enfermaria,
                tl.dsc_leito,
                mov.isn_destino,
                mov.prox_clinica,
                COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date AS dt_entrada,
                COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date AS dt_saida,
                mov.dat_alta::date AS dt_alta
            FROM movimentos mov
            JOIN t_clinica tc ON mov.isn_clinica = tc.isn_clinica
            JOIN t_enfermaria te ON mov.isn_enfermaria = te.isn_enfermaria
            JOIN t_leito tl ON mov.isn_leito = tl.isn_leito
            LEFT JOIN t_destino td ON mov.isn_destino = td.isn_destino
            WHERE mov.isn_clinica IN ({CLINICAS_TMP_PS_SQL})
              AND mov.isn_leito NOT IN ({LEITOS_TMP_PS_EXCL_SQL})
              AND COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date <= (SELECT dt_fim FROM limites)
              AND COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date >= (SELECT dt_ini FROM limites)
              {filt_sql}
        ),
        partes AS (
            SELECT
                b.id_mov,
                gs.mes::date AS mes,
                b.isn_destino,
                b.dt_entrada,
                b.dt_saida,
                GREATEST(b.dt_entrada, gs.mes::date, (SELECT dt_ini FROM limites)) AS ini,
                LEAST(b.dt_saida, (gs.mes + INTERVAL '1 month')::date, (SELECT dt_fim FROM limites) + 1) AS fim  -- fim exclusivo: a noite da virada do mês conta no mês em que começa
            FROM base b
            CROSS JOIN LATERAL generate_series(
                DATE_TRUNC('month', GREATEST(b.dt_entrada, (SELECT dt_ini FROM limites)))::date,
                DATE_TRUNC('month', LEAST(b.dt_saida, (SELECT dt_fim FROM limites)))::date,
                INTERVAL '1 month'
            ) AS gs(mes)
        ),
        paciente_dia_mes AS (
            SELECT
                mes,
                SUM(
{paciente_dia_expr}
                )::numeric AS paciente_dia
            FROM partes
            GROUP BY mes
        ),
        saidas_mes AS (
            SELECT
                DATE_TRUNC('month', dt_alta)::date AS mes,
                SUM(CASE WHEN {saida_valida_expr} THEN 1 ELSE 0 END)::numeric AS saidas
            FROM base
            WHERE dt_alta BETWEEN (SELECT dt_ini FROM limites) AND (SELECT dt_fim FROM limites)
            GROUP BY DATE_TRUNC('month', dt_alta)::date
        )
        SELECT
            pd.mes,
            pd.paciente_dia,
            sm.saidas,
            ROUND(pd.paciente_dia / NULLIF(sm.saidas, 0), 2) AS tmp_ps
        FROM paciente_dia_mes pd
        LEFT JOIN saidas_mes sm ON sm.mes = pd.mes
        ORDER BY pd.mes
    """, [dt_ini, dt_fim] + filt_vals)
    mapa_tmp_ps = {r["mes"]: r["tmp_ps"] for r in (rows_tmp_ps or [])}
    mapa_paciente_dia_tmp_ps = {r["mes"]: r["paciente_dia"] for r in (rows_tmp_ps or [])}
    mapa_saidas_tmp_ps = {r["mes"]: r["saidas"] for r in (rows_tmp_ps or [])}

    rows_saidas_tmp_ps_destino_mes = fetch_all(f"""
        WITH limites AS (
            SELECT
                %s::date AS dt_ini,
                %s::date AS dt_fim
        ),
        movimentos AS (
            SELECT
                til.*,
                LEAD(til.isn_clinica) OVER (
                    PARTITION BY til.isn_internacao
                    ORDER BY COALESCE(til.dat_internacao_info, til.dat_internacao), til.isn_internacao_leito
                ) AS prox_clinica
            FROM t_internacao_leito til
        ),
        base AS (
            SELECT
                mov.isn_clinica,
                tc.dsc_clinica,
                te.dsc_enfermaria,
                tl.dsc_leito,
                mov.isn_destino,
                td.dsc_destino,
                mov.prox_clinica,
                COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date AS dt_entrada,
                COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date AS dt_saida,
                mov.dat_alta::date AS dt_alta
            FROM movimentos mov
            JOIN t_clinica tc ON mov.isn_clinica = tc.isn_clinica
            JOIN t_enfermaria te ON mov.isn_enfermaria = te.isn_enfermaria
            JOIN t_leito tl ON mov.isn_leito = tl.isn_leito
            LEFT JOIN t_destino td ON mov.isn_destino = td.isn_destino
            WHERE mov.isn_clinica IN ({CLINICAS_TMP_PS_SQL})
              AND mov.isn_leito NOT IN ({LEITOS_TMP_PS_EXCL_SQL})
              AND COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date <= (SELECT dt_fim FROM limites)
              AND COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date >= (SELECT dt_ini FROM limites)
              {filt_sql}
        )
        SELECT
            DATE_TRUNC('month', dt_alta)::date AS mes,
            isn_destino,
            COALESCE(dsc_destino, 'Destino ' || isn_destino::text) AS dsc_destino,
            COUNT(*)::numeric AS qtd
        FROM base
        WHERE {saida_valida_expr}
          AND dt_alta BETWEEN (SELECT dt_ini FROM limites) AND (SELECT dt_fim FROM limites)
        GROUP BY DATE_TRUNC('month', dt_alta)::date, isn_destino, dsc_destino
        ORDER BY mes, isn_destino
    """, [dt_ini, dt_fim] + filt_vals)

    rows_internacoes_ps = fetch_all("""
        WITH internacoes_ps AS (
            SELECT
                ti.isn_internacao,
                MIN(COALESCE(til.dat_alta_info, til.dat_alta)::date) AS dt_ref
            FROM t_internacao ti
            JOIN t_internacao_leito til ON til.isn_internacao = ti.isn_internacao
            WHERE ti.isn_tipo_atendimento = 2
              AND til.isn_destino = 13
              AND COALESCE(til.dat_alta_info, til.dat_alta)::date BETWEEN %s AND %s
            GROUP BY ti.isn_internacao
        )
        SELECT
            DATE_TRUNC('month', dt_ref)::date AS mes,
            COUNT(*)::integer AS internacoes_ps
        FROM internacoes_ps
        GROUP BY DATE_TRUNC('month', dt_ref)::date
        ORDER BY mes
    """, [dt_ini, dt_fim])
    mapa_internacoes_ps = {r["mes"]: r["internacoes_ps"] for r in (rows_internacoes_ps or [])}

    _leitos_ps_filt_sql, _leitos_ps_filt_vals = _filtros_sql(clinica, enfermaria, leito, prefixo="AND")

    rows_leitos_ps_mensais = fetch_all(f"""
        WITH
        limites AS (SELECT %s::date AS dt_ini, %s::date AS dt_fim),
        meses AS (
            SELECT generate_series(
                DATE_TRUNC('month', (SELECT dt_ini FROM limites))::date,
                DATE_TRUNC('month', (SELECT dt_fim FROM limites))::date,
                INTERVAL '1 month'
            )::date AS mes
        ),
        dias_por_mes AS (
            SELECT
                m.mes,
                generate_series(
                    GREATEST(m.mes, (SELECT dt_ini FROM limites)),
                    LEAST((m.mes + INTERVAL '1 month' - INTERVAL '1 day')::date, (SELECT dt_fim FROM limites)),
                    INTERVAL '1 day'
                )::date AS dt
            FROM meses m
        ),
        leitos_base_det AS (
            SELECT tl.isn_leito, tc.dsc_clinica, te.dsc_enfermaria, tl.dsc_leito
            FROM t_leito tl
            JOIN t_enfermaria te ON te.isn_enfermaria = tl.isn_enfermaria
            JOIN t_clinica tc ON tc.isn_clinica = te.isn_clinica
            WHERE tl.flg_ativo = 'S'
              AND tl.flg_extra = 'N'
              AND te.flg_ativo = 'S'
              AND tc.flg_ativo = 'S'
              AND te.isn_clinica IN ({CLINICAS_PS_SQL})
              AND tl.isn_leito NOT IN ({LEITOS_TMP_PS_EXCL_SQL})
              {_leitos_ps_filt_sql}
        ),
        leitos_base AS (
            SELECT COUNT(*) AS total FROM leitos_base_det
        ),
        interdicoes_por_dia AS (
            SELECT
                dpm.dt,
                COUNT(DISTINCT lbd.isn_leito) AS interditados
            FROM dias_por_mes dpm
            CROSS JOIN leitos_base_det lbd
            WHERE EXISTS (
                SELECT 1 FROM t_leito_interditado tli
                WHERE tli.isn_leito = lbd.isn_leito
                  AND tli.dat_interdicao::date <= dpm.dt
                  AND (tli.dat_liberacao IS NULL OR tli.dat_liberacao::date > dpm.dt)
                  AND EXTRACT(EPOCH FROM (COALESCE(tli.dat_liberacao, CURRENT_TIMESTAMP) - tli.dat_interdicao)) > 86400
            )
            GROUP BY dpm.dt
        )
        SELECT
            dpm.mes,
            ROUND(AVG(lb.total - COALESCE(ipd.interditados, 0))::numeric, 2) AS media_disponiveis
        FROM dias_por_mes dpm
        CROSS JOIN leitos_base lb
        LEFT JOIN interdicoes_por_dia ipd ON ipd.dt = dpm.dt
        GROUP BY dpm.mes, lb.total
        ORDER BY dpm.mes
    """, [dt_ini, dt_fim] + _leitos_ps_filt_vals)
    mapa_leitos_ps_mensais = {r["mes"]: r["media_disponiveis"] for r in (rows_leitos_ps_mensais or [])}

    result = {
        "meses": _meses_labels(meses_keys),
        "meses_iso": _meses_iso(meses_keys),
        "los_sem": [],
        "los_com": [],
        "tmp_ps": [],
        "paciente_dia_tmp_ps": [],
        "saidas_tmp_ps": [],
        "saidas_tmp_ps_destinos_mensais": {},
        "giro_ps": [],
        "porta_medico": [],
        "internacoes_ps": [],
    }

    mapa_saidas_tmp_ps_dest = {}
    for r in (rows_saidas_tmp_ps_destino_mes or []):
        k = str(r["isn_destino"])
        if k not in mapa_saidas_tmp_ps_dest:
            mapa_saidas_tmp_ps_dest[k] = {
                "isn_destino": r["isn_destino"],
                "dsc_destino": r["dsc_destino"],
                "por_mes": {}
            }
        mapa_saidas_tmp_ps_dest[k]["por_mes"][r["mes"]] = r["qtd"]

    for mes in meses_keys:
        leitos_ps_mes = mapa_leitos_ps_mensais.get(mes)
        saidas_tmp_ps_mes = mapa_saidas_tmp_ps.get(mes)

        result["los_sem"].append(mapa_los_sem.get(mes))
        result["los_com"].append(mapa_los_com.get(mes))
        result["tmp_ps"].append(mapa_tmp_ps.get(mes))
        result["paciente_dia_tmp_ps"].append(mapa_paciente_dia_tmp_ps.get(mes))
        result["saidas_tmp_ps"].append(saidas_tmp_ps_mes)
        result["giro_ps"].append(
            round(float(saidas_tmp_ps_mes) / float(leitos_ps_mes), 2)
            if saidas_tmp_ps_mes is not None and leitos_ps_mes else None
        )
        result["porta_medico"].append(mapa_porta_medico.get(mes))
        result["internacoes_ps"].append(mapa_internacoes_ps.get(mes))

    for k, info in sorted(mapa_saidas_tmp_ps_dest.items(), key=lambda x: int(x[0])):
        result["saidas_tmp_ps_destinos_mensais"][k] = {
            "isn_destino": info["isn_destino"],
            "dsc_destino": info["dsc_destino"],
            "valores": [info["por_mes"].get(mes, 0) for mes in meses_keys]
        }

    return result


# ---------------------------------------------------------------------------
# INDICADORES HOSPITALARES / EMERGÊNCIA — SNAPSHOT DE UM PERÍODO
# (equivalente ao antigo /api/indicadores do painel LEAN, mas dividido em
# dois blocos independentes, cada um só rodando as consultas do seu bloco)
# ---------------------------------------------------------------------------

def get_leito_dia_periodo(clinica, enfermaria, leito, data_ini, data_fim):
    """
    Leito-dia do período: soma, dia a dia, dos leitos disponíveis
    (flg_ativo='S' e flg_extra='N'), descontando apenas interdições com
    duração > 5 dias (por data; o dia da liberação já conta como
    disponível).
    """
    filt_sql, filt_vals = _filtros_sql(clinica, enfermaria, leito, prefixo="AND")

    row = fetch_one(f"""
        WITH
        dias AS (
            SELECT generate_series(%s::date, %s::date, INTERVAL '1 day')::date AS dt
        ),
        leitos_base_det AS (
            SELECT tl.isn_leito
            FROM t_leito tl
            JOIN t_enfermaria te ON te.isn_enfermaria = tl.isn_enfermaria
            JOIN t_clinica tc ON tc.isn_clinica = te.isn_clinica
            WHERE tl.flg_ativo = 'S'
              AND tl.flg_extra = 'N'
              AND te.flg_ativo = 'S'
              AND tc.flg_ativo = 'S'
              AND te.isn_clinica IN ({CLINICAS_TMPH_SQL})
              {filt_sql}
        ),
        leitos_base AS (
            SELECT COUNT(*) AS total FROM leitos_base_det
        ),
        interdicoes_por_dia AS (
            SELECT
                d.dt,
                COUNT(DISTINCT lbd.isn_leito) AS interditados
            FROM dias d
            CROSS JOIN leitos_base_det lbd
            WHERE EXISTS (
                SELECT 1
                FROM t_leito_interditado tli
                WHERE tli.isn_leito = lbd.isn_leito
                  AND tli.dat_interdicao::date <= d.dt
                  AND (tli.dat_liberacao IS NULL OR tli.dat_liberacao::date > d.dt)
                  AND (COALESCE(tli.dat_liberacao::date, CURRENT_DATE) - tli.dat_interdicao::date) > 5
            )
            GROUP BY d.dt
        )
        SELECT
            lb.total                                              AS leitos_base,
            COUNT(d.dt)                                            AS dias,
            SUM(lb.total - COALESCE(ipd.interditados, 0))::bigint  AS leito_dia
        FROM dias d
        CROSS JOIN leitos_base lb
        LEFT JOIN interdicoes_por_dia ipd ON ipd.dt = d.dt
        GROUP BY lb.total
    """, [data_ini, data_fim] + filt_vals)

    leitos_base = row["leitos_base"] if row else 0
    dias = row["dias"] if row else 0
    leito_dia = row["leito_dia"] if row and row["leito_dia"] is not None else 0
    return leitos_base, leito_dia, dias


def get_indicadores_hospitalar(data_ini, data_fim, clinica, enfermaria, leito):
    """Snapshot de um período: paciente-dia, saídas, TMP Hospitalar, giro de
    leito, leito-dia, taxa de ocupação, internações e leitos disponíveis."""
    filt_sql, filt_vals = _filtros_sql(clinica, enfermaria, leito)
    _clinica_unica = (isinstance(clinica, str) and clinica) or (isinstance(clinica, list) and len(clinica) == 1)
    incluir_destino_5 = "TRUE" if _clinica_unica else "FALSE"

    paciente_dia_expr = """
                CASE
                    WHEN fim > ini THEN (fim - ini)::numeric
                    WHEN fim = ini AND dt_entrada = dt_saida AND COALESCE(isn_destino, 0) <> 1 THEN 1::numeric
                    ELSE 0::numeric
                END
    """
    saida_valida_expr = f"""
                (
                    isn_destino IN (1, 3, 4)
                    OR (
                        {incluir_destino_5}
                        AND isn_destino = 5
                        AND (
                            isn_clinica <> 22
                            OR prox_clinica IN (40, 48, 15, 24)
                        )
                    )
                )
    """

    tmph = fetch_one(f"""
        WITH movimentos AS (
            SELECT
                til.*,
                LEAD(til.isn_clinica) OVER (
                    PARTITION BY til.isn_internacao
                    ORDER BY COALESCE(til.dat_internacao_info, til.dat_internacao), til.isn_internacao_leito
                ) AS prox_clinica
            FROM t_internacao_leito til
        ),
        base AS (
            SELECT
                mov.isn_internacao_leito AS id_mov,
                mov.isn_clinica,
                tc.dsc_clinica,
                te.dsc_enfermaria,
                tl.dsc_leito,
                mov.isn_destino,
                mov.prox_clinica,
                COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date AS dt_entrada,
                COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date AS dt_saida,
                mov.dat_alta::date AS dt_alta
            FROM movimentos mov
            JOIN t_clinica tc ON mov.isn_clinica = tc.isn_clinica
            JOIN t_enfermaria te ON mov.isn_enfermaria = te.isn_enfermaria
            JOIN t_leito tl ON mov.isn_leito = tl.isn_leito
            LEFT JOIN t_destino td ON mov.isn_destino = td.isn_destino
            WHERE mov.isn_clinica IN ({CLINICAS_TMPH_SQL})
              AND COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date <= %s
              AND COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date >= %s
              {filt_sql}
        ),
        partes AS (
            SELECT
                b.id_mov,
                b.isn_destino,
                b.dt_entrada,
                b.dt_saida,
                GREATEST(b.dt_entrada, gs.mes::date, %s::date) AS ini,
                LEAST(b.dt_saida, (gs.mes + INTERVAL '1 month')::date, %s::date + 1) AS fim  -- fim exclusivo: a noite da virada do mês conta no mês em que começa
            FROM base b
            CROSS JOIN LATERAL generate_series(
                DATE_TRUNC('month', GREATEST(b.dt_entrada, %s::date))::date,
                DATE_TRUNC('month', LEAST(b.dt_saida, %s::date))::date,
                INTERVAL '1 month'
            ) AS gs(mes)
        ),
        aggr AS (
            SELECT SUM(
{paciente_dia_expr}
            )::numeric AS paciente_dia
            FROM partes
        ),
        saidas AS (
            SELECT SUM(
                CASE WHEN {saida_valida_expr} AND dt_alta BETWEEN %s AND %s THEN 1 ELSE 0 END
            )::numeric AS saidas
            FROM base
        )
        SELECT
            aggr.paciente_dia,
            saidas.saidas,
            ROUND(aggr.paciente_dia / NULLIF(saidas.saidas, 0), 2) AS valor
        FROM aggr
        CROSS JOIN saidas
    """, [data_fim, data_ini] + filt_vals + [data_ini, data_fim, data_ini, data_fim, data_ini, data_fim])

    qtd_leitos_dia, leito_dia, dias_periodo = get_leito_dia_periodo(clinica, enfermaria, leito, data_ini, data_fim)

    _paciente_dia_val = tmph["paciente_dia"] if tmph and tmph["paciente_dia"] is not None else None
    taxa_ocupacao = (
        round(float(_paciente_dia_val) / leito_dia * 100, 2)
        if _paciente_dia_val and leito_dia
        else None
    )

    saidas_detalhe = fetch_all(f"""
        WITH movimentos AS (
            SELECT
                til.*,
                LEAD(til.isn_clinica) OVER (
                    PARTITION BY til.isn_internacao
                    ORDER BY COALESCE(til.dat_internacao_info, til.dat_internacao), til.isn_internacao_leito
                ) AS prox_clinica
            FROM t_internacao_leito til
        ),
        base AS (
            SELECT
                mov.isn_internacao,
                mov.isn_clinica,
                tc.dsc_clinica,
                te.dsc_enfermaria,
                tl.dsc_leito,
                mov.isn_destino,
                td.dsc_destino,
                mov.prox_clinica,
                COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date AS dt_entrada,
                COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date AS dt_saida,
                mov.dat_alta::date AS dt_alta,
                COALESCE(
                    mov.dat_alta_info + COALESCE(mov.hor_alta_info, '00:00')::time,
                    mov.dat_alta      + COALESCE(mov.hor_alta, '00:00')::time
                ) AS ts_alta
            FROM movimentos mov
            JOIN t_clinica tc ON mov.isn_clinica = tc.isn_clinica
            JOIN t_enfermaria te ON mov.isn_enfermaria = te.isn_enfermaria
            JOIN t_leito tl ON mov.isn_leito = tl.isn_leito
            LEFT JOIN t_destino td ON mov.isn_destino = td.isn_destino
            WHERE mov.isn_clinica IN ({CLINICAS_TMPH_SQL})
              AND COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date <= %s
              AND COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date >= %s
              {filt_sql}
        ),
        base_ti AS (
            SELECT b.*, ti.dat_cadastro
            FROM base b
            JOIN t_internacao ti ON ti.isn_internacao = b.isn_internacao
        ),
        base_rotulo AS (
            SELECT
                *,
                CASE
                    WHEN isn_destino = 4
                         AND ts_alta IS NOT NULL AND dat_cadastro IS NOT NULL
                         AND EXTRACT(EPOCH FROM (ts_alta - dat_cadastro)) > 86400
                        THEN 'Óbito Institucional'
                    WHEN isn_destino = 4
                        THEN 'Óbito Hospitalar'
                    ELSE COALESCE(dsc_destino, 'Destino ' || isn_destino::text)
                END AS rotulo_destino
            FROM base_ti
        )
        SELECT
            isn_destino,
            rotulo_destino AS dsc_destino,
            COUNT(*)::numeric AS qtd
        FROM base_rotulo
                WHERE {saida_valida_expr}
                    AND dt_alta BETWEEN %s AND %s
        GROUP BY isn_destino, rotulo_destino
        ORDER BY isn_destino
    """, [data_fim, data_ini] + filt_vals + [data_ini, data_fim])

    total_obitos = sum(
        float(r["qtd"]) for r in (saidas_detalhe or [])
        if r["dsc_destino"] in ("Óbito Institucional", "Óbito Hospitalar")
    )
    obitos_institucionais = sum(
        float(r["qtd"]) for r in (saidas_detalhe or [])
        if r["dsc_destino"] == "Óbito Institucional"
    )
    _saidas_val_mortalidade = tmph["saidas"] if tmph and tmph["saidas"] is not None else None
    taxa_mortalidade_hospitalar = (
        round(total_obitos / float(_saidas_val_mortalidade) * 100, 2)
        if _saidas_val_mortalidade else None
    )
    taxa_mortalidade_institucional = (
        round(obitos_institucionais / float(_saidas_val_mortalidade) * 100, 2)
        if _saidas_val_mortalidade else None
    )

    _leitos_filt_sql, _leitos_filt_vals = _filtros_sql(clinica, None, None, prefixo="AND")

    leitos_ativos_row = fetch_one(f"""
        WITH
        dias AS (
            SELECT generate_series(%s::date, %s::date, INTERVAL '1 day')::date AS dt
        ),
        leitos_base_det AS (
            SELECT te.isn_clinica, tc.dsc_clinica, tl.isn_leito
            FROM t_leito tl
            JOIN t_enfermaria te ON te.isn_enfermaria = tl.isn_enfermaria
            JOIN t_clinica tc ON tc.isn_clinica = te.isn_clinica
            WHERE tl.flg_ativo = 'S'
              AND tl.flg_extra = 'N'
              AND te.flg_ativo = 'S'
              AND tc.flg_ativo = 'S'
              AND te.isn_clinica IN ({CLINICAS_INTERNACOES_SQL})
              {_leitos_filt_sql}
        ),
        leitos_base AS (
            SELECT COUNT(*) AS total
            FROM leitos_base_det
        ),
        interdicoes_por_dia AS (
            SELECT
                d.dt,
                COUNT(DISTINCT lbd.isn_leito) AS interditados
            FROM dias d
            CROSS JOIN leitos_base_det lbd
            WHERE EXISTS (
                SELECT 1
                FROM t_leito_interditado tli
                WHERE tli.isn_leito = lbd.isn_leito
                  AND tli.dat_interdicao::date <= d.dt
                  AND (tli.dat_liberacao IS NULL OR tli.dat_liberacao::date > d.dt)
                  AND EXTRACT(EPOCH FROM (COALESCE(tli.dat_liberacao, CURRENT_TIMESTAMP) - tli.dat_interdicao)) > 86400
            )
            GROUP BY d.dt
        )
        SELECT CEIL(AVG(lb.total - COALESCE(ipd.interditados, 0))::numeric)::integer AS total
        FROM dias d
        CROSS JOIN leitos_base lb
        LEFT JOIN interdicoes_por_dia ipd ON ipd.dt = d.dt
    """, [data_ini, data_fim] + _leitos_filt_vals)
    leitos_ativos_total = leitos_ativos_row["total"] if leitos_ativos_row else None

    leitos_ativos_detalhe = fetch_all(f"""
        WITH
        dias AS (
            SELECT generate_series(%s::date, %s::date, INTERVAL '1 day')::date AS dt
        ),
        leitos_base_det AS (
            SELECT te.isn_clinica, tc.dsc_clinica, tl.isn_leito
            FROM t_leito tl
            JOIN t_enfermaria te ON te.isn_enfermaria = tl.isn_enfermaria
            JOIN t_clinica tc ON tc.isn_clinica = te.isn_clinica
            WHERE tl.flg_ativo = 'S'
              AND tl.flg_extra = 'N'
              AND te.flg_ativo = 'S'
              AND tc.flg_ativo = 'S'
              AND te.isn_clinica IN ({CLINICAS_INTERNACOES_SQL})
              {_leitos_filt_sql}
        ),
        leitos_por_clinica AS (
            SELECT isn_clinica, dsc_clinica, COUNT(*) AS total
            FROM leitos_base_det
            GROUP BY isn_clinica, dsc_clinica
        ),
        interdicoes_por_clinica_dia AS (
            SELECT
                d.dt,
                lbd.isn_clinica,
                COUNT(DISTINCT lbd.isn_leito) AS interditados
            FROM dias d
            CROSS JOIN leitos_base_det lbd
            WHERE EXISTS (
                SELECT 1 FROM t_leito_interditado tli
                WHERE tli.isn_leito = lbd.isn_leito
                  AND tli.dat_interdicao::date <= d.dt
                  AND (tli.dat_liberacao IS NULL OR tli.dat_liberacao::date > d.dt)
                  AND EXTRACT(EPOCH FROM (COALESCE(tli.dat_liberacao, CURRENT_TIMESTAMP) - tli.dat_interdicao)) > 86400
            )
            GROUP BY d.dt, lbd.isn_clinica
        )
        SELECT
            lpc.dsc_clinica,
            CEIL(AVG(lpc.total - COALESCE(ipcd.interditados, 0))::numeric)::integer AS qtd
        FROM leitos_por_clinica lpc
        CROSS JOIN dias d
        LEFT JOIN interdicoes_por_clinica_dia ipcd
            ON ipcd.isn_clinica = lpc.isn_clinica AND ipcd.dt = d.dt
        GROUP BY lpc.isn_clinica, lpc.dsc_clinica, lpc.total
        ORDER BY qtd DESC
    """, [data_ini, data_fim] + _leitos_filt_vals)

    internacoes_row = fetch_one(_PRIMEIRA_CLINICA_CTE + f"""
        SELECT COUNT(DISTINCT ti.isn_internacao) AS total
        FROM t_internacao ti
        JOIN primeira_clinica pc ON pc.isn_internacao = ti.isn_internacao
        WHERE ti.isn_tipo_atendimento = 1
          AND ti.dat_cadastro::date BETWEEN %s AND %s
          AND pc.isn_clinica IN ({CLINICAS_INTERNACOES_SQL})
        {filt_sql}
    """, [data_ini, data_fim] + filt_vals)

    internacoes_detalhe = fetch_all(_PRIMEIRA_CLINICA_CTE + f"""
        SELECT pc.dsc_clinica, COUNT(DISTINCT ti.isn_internacao)::integer AS qtd
        FROM t_internacao ti
        JOIN primeira_clinica pc ON pc.isn_internacao = ti.isn_internacao
        WHERE ti.isn_tipo_atendimento = 1
          AND ti.dat_cadastro::date BETWEEN %s AND %s
          AND pc.isn_clinica IN ({CLINICAS_INTERNACOES_SQL})
        {filt_sql}
        GROUP BY pc.dsc_clinica
        ORDER BY qtd DESC
    """, [data_ini, data_fim] + filt_vals)

    saidas_val = tmph["saidas"] if tmph and tmph["saidas"] is not None else None
    giro = round(float(saidas_val) / leitos_ativos_total, 2) if saidas_val and leitos_ativos_total else None

    return {
        "paciente_dia": tmph["paciente_dia"] if tmph else None,
        "saidas": tmph["saidas"] if tmph else None,
        "saidas_detalhe": [dict(r) for r in (saidas_detalhe or [])],
        "total_obitos": total_obitos,
        "obitos_institucionais": obitos_institucionais,
        "taxa_mortalidade_hospitalar": taxa_mortalidade_hospitalar,
        "taxa_mortalidade_institucional": taxa_mortalidade_institucional,
        "tmph": tmph["valor"] if tmph else None,
        "leito_dia": leito_dia,
        "qtd_leitos_leito_dia": qtd_leitos_dia,
        "dias_periodo": dias_periodo,
        "taxa_ocupacao": taxa_ocupacao,
        "giro": giro,
        "internacoes": internacoes_row["total"] if internacoes_row else None,
        "internacoes_detalhe": [dict(r) for r in (internacoes_detalhe or [])],
        "leitos_ativos": leitos_ativos_total,
        "leitos_ativos_detalhe": [dict(r) for r in (leitos_ativos_detalhe or [])],
    }


def get_indicadores_emergencia(data_ini, data_fim, clinica, enfermaria, leito):
    """Snapshot de um período: LOS sem/com internação, tempo porta-médico,
    TMP leitos PS, saídas PS, giro de leito PS, internações PS e leitos
    disponíveis PS."""
    filt_sql, filt_vals = _filtros_sql(clinica, enfermaria, leito)
    _clinica_unica = (isinstance(clinica, str) and clinica) or (isinstance(clinica, list) and len(clinica) == 1)
    incluir_destino_5 = "TRUE" if _clinica_unica else "FALSE"

    paciente_dia_expr = """
                CASE
                    WHEN fim > ini THEN (fim - ini)::numeric
                    WHEN fim = ini AND dt_entrada = dt_saida AND COALESCE(isn_destino, 0) <> 1 THEN 1::numeric
                    ELSE 0::numeric
                END
    """
    saida_valida_expr = f"""
                (
                    isn_destino IN (1, 3, 4)
                    OR (
                        {incluir_destino_5}
                        AND isn_destino = 5
                        AND (
                            isn_clinica <> 22
                            OR prox_clinica IN (40, 48, 15, 24)
                        )
                    )
                )
    """

    los_sem = fetch_one("""
        SELECT ROUND(
            AVG(EXTRACT(EPOCH FROM (
                COALESCE(til.dat_alta_info  + til.hor_alta_info::time,
                         til.dat_alta       + til.hor_alta::time)
                -
                COALESCE(til.dat_internacao_info + til.hor_internacao_info::time,
                         til.dat_internacao      + til.hor_internacao::time)
            )) / 60)::numeric, 2
        ) AS valor
        FROM t_internacao ti
        LEFT JOIN t_tipo_atendimento tta ON ti.isn_tipo_atendimento = tta.isn_tipo_atendimento
        LEFT JOIN t_internacao_leito til ON ti.isn_internacao = til.isn_internacao
        JOIN  t_paciente tp ON ti.isn_paciente  = tp.isn_paciente
        JOIN  t_destino  td ON til.isn_destino  = td.isn_destino
        WHERE ti.isn_tipo_atendimento = 2
          AND til.isn_destino IN (1, 3, 4)
          AND til.dat_alta IS NOT NULL
          AND ti.dat_cadastro::date BETWEEN %s AND %s
    """, [data_ini, data_fim])

    los_com = fetch_one(f"""
        WITH
        internacao_datas AS (
            SELECT
                ti.isn_internacao,
                tp.num_prontuario,
                MIN(til.dat_internacao) AS dat_entrada,
                MAX(til.dat_alta)       AS dat_saida
            FROM t_internacao ti
            JOIN t_paciente tp ON tp.isn_paciente = ti.isn_paciente
            JOIN t_internacao_leito til ON til.isn_internacao = ti.isn_internacao
            WHERE til.dat_internacao IS NOT NULL
            GROUP BY ti.isn_internacao, tp.num_prontuario
        ),
        continuidade AS (
            SELECT
                isn_internacao,
                num_prontuario,
                dat_entrada,
                dat_saida,
                CASE
                    WHEN LAG(dat_saida) OVER (
                        PARTITION BY num_prontuario ORDER BY dat_entrada, isn_internacao
                    ) = dat_entrada
                    THEN 0 ELSE 1
                END AS novo_grupo
            FROM internacao_datas
        ),
        grupos AS (
            SELECT
                isn_internacao,
                num_prontuario,
                dat_entrada,
                SUM(novo_grupo) OVER (
                    PARTITION BY num_prontuario ORDER BY dat_entrada, isn_internacao
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS grp_num
            FROM continuidade
        ),
        cadeia AS (
            SELECT
                g.isn_internacao,
                FIRST_VALUE(g.isn_internacao) OVER (
                    PARTITION BY g.num_prontuario, g.grp_num
                    ORDER BY g.dat_entrada, g.isn_internacao
                ) AS id_grupo
            FROM grupos g
        ),
        base AS (
            SELECT
                c.id_grupo,
                til.isn_clinica,
                COALESCE(
                    til.dat_internacao_info + til.hor_internacao_info::time,
                    til.dat_internacao + til.hor_internacao::time
                ) AS ts_internacao,
                COALESCE(
                    til.dat_alta_info + til.hor_alta_info::time,
                    til.dat_alta + til.hor_alta::time
                ) AS ts_alta
            FROM cadeia c
            JOIN t_internacao_leito til ON til.isn_internacao = c.isn_internacao
            WHERE til.dat_internacao IS NOT NULL
        ),
        grupo_inicio AS (
            SELECT
                b.id_grupo,
                MIN(b.ts_internacao) AS ts_clinica_1
            FROM base b
            JOIN t_internacao ti ON ti.isn_internacao = b.id_grupo
            JOIN (
                SELECT DISTINCT ON (isn_internacao) isn_internacao, isn_destino
                FROM t_internacao_leito
                ORDER BY isn_internacao,
                    COALESCE(dat_alta_info + hor_alta_info::time, dat_alta + hor_alta::time) DESC NULLS LAST,
                    isn_internacao_leito DESC
            ) til_ult ON til_ult.isn_internacao = b.id_grupo
            WHERE b.isn_clinica = 1
              AND ti.isn_tipo_atendimento = 2
              AND til_ult.isn_destino = 13
              AND ti.dat_cadastro::date BETWEEN %s AND %s
            GROUP BY b.id_grupo
        ),
        grupo_destino AS (
            SELECT
                b.id_grupo,
                MIN(b.ts_internacao) AS ts_primeira_clinica
            FROM base b
            JOIN grupo_inicio g ON g.id_grupo = b.id_grupo
            WHERE b.isn_clinica IN ({CLINICAS_LOS_COM_DEST_SQL})
              AND b.ts_internacao IS NOT NULL
              AND b.ts_internacao >= g.ts_clinica_1
            GROUP BY b.id_grupo
        ),
        grupo_fallback AS (
            SELECT
                b.id_grupo,
                MAX(COALESCE(b.ts_alta, CURRENT_TIMESTAMP)) AS ts_ultima_alta
            FROM base b
            JOIN grupo_inicio g ON g.id_grupo = b.id_grupo
            WHERE b.ts_internacao >= g.ts_clinica_1
            GROUP BY b.id_grupo
        ),
        grupo_final AS (
            SELECT
                g.id_grupo,
                g.ts_clinica_1,
                COALESCE(d.ts_primeira_clinica, f.ts_ultima_alta) AS ts_referencia
            FROM grupo_inicio g
            LEFT JOIN grupo_destino d ON d.id_grupo = g.id_grupo
            LEFT JOIN grupo_fallback f ON f.id_grupo = g.id_grupo
        ),
        los_por_grupo AS (
            SELECT
                gf.id_grupo,
                EXTRACT(EPOCH FROM (gf.ts_referencia - gf.ts_clinica_1)) / 60 AS los_min
            FROM grupo_final gf
            WHERE gf.ts_referencia > gf.ts_clinica_1
        )
        SELECT ROUND(AVG(lpg.los_min)::numeric, 2) AS valor
        FROM los_por_grupo lpg
    """, [data_ini, data_fim])

    tmp_ps = fetch_one(f"""
        WITH movimentos AS (
            SELECT
                til.*,
                LEAD(til.isn_clinica) OVER (
                    PARTITION BY til.isn_internacao
                    ORDER BY COALESCE(til.dat_internacao_info, til.dat_internacao), til.isn_internacao_leito
                ) AS prox_clinica
            FROM t_internacao_leito til
        ),
        base AS (
            SELECT
                mov.isn_internacao_leito AS id_mov,
                mov.isn_clinica,
                tc.dsc_clinica,
                te.dsc_enfermaria,
                tl.dsc_leito,
                mov.isn_destino,
                mov.prox_clinica,
                COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date AS dt_entrada,
                COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date AS dt_saida,
                mov.dat_alta::date AS dt_alta
            FROM movimentos mov
            JOIN t_clinica tc ON mov.isn_clinica = tc.isn_clinica
            JOIN t_enfermaria te ON mov.isn_enfermaria = te.isn_enfermaria
            JOIN t_leito tl ON mov.isn_leito = tl.isn_leito
            LEFT JOIN t_destino td ON mov.isn_destino = td.isn_destino
            WHERE mov.isn_clinica IN ({CLINICAS_TMP_PS_SQL})
              AND mov.isn_leito NOT IN ({LEITOS_TMP_PS_EXCL_SQL})
              AND COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date <= %s
              AND COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date >= %s
              {filt_sql}
        ),
        partes AS (
            SELECT
                b.id_mov,
                b.isn_destino,
                b.dt_entrada,
                b.dt_saida,
                GREATEST(b.dt_entrada, gs.mes::date, %s::date) AS ini,
                LEAST(b.dt_saida, (gs.mes + INTERVAL '1 month')::date, %s::date + 1) AS fim  -- fim exclusivo: a noite da virada do mês conta no mês em que começa
            FROM base b
            CROSS JOIN LATERAL generate_series(
                DATE_TRUNC('month', GREATEST(b.dt_entrada, %s::date))::date,
                DATE_TRUNC('month', LEAST(b.dt_saida, %s::date))::date,
                INTERVAL '1 month'
            ) AS gs(mes)
        ),
        aggr AS (
            SELECT SUM(
{paciente_dia_expr}
            )::numeric AS paciente_dia
            FROM partes
        ),
        saidas AS (
            SELECT SUM(
                CASE WHEN {saida_valida_expr} AND dt_alta BETWEEN %s AND %s THEN 1 ELSE 0 END
            )::numeric AS saidas
            FROM base
        )
        SELECT
            aggr.paciente_dia,
            saidas.saidas,
            ROUND(aggr.paciente_dia / NULLIF(saidas.saidas, 0), 2) AS valor
        FROM aggr
        CROSS JOIN saidas
    """, [data_fim, data_ini] + filt_vals + [data_ini, data_fim, data_ini, data_fim, data_ini, data_fim])

    saidas_tmp_ps_detalhe = fetch_all(f"""
        WITH movimentos AS (
            SELECT
                til.*,
                LEAD(til.isn_clinica) OVER (
                    PARTITION BY til.isn_internacao
                    ORDER BY COALESCE(til.dat_internacao_info, til.dat_internacao), til.isn_internacao_leito
                ) AS prox_clinica
            FROM t_internacao_leito til
        ),
        base AS (
            SELECT
                mov.isn_clinica,
                tc.dsc_clinica,
                te.dsc_enfermaria,
                tl.dsc_leito,
                mov.isn_destino,
                td.dsc_destino,
                mov.prox_clinica,
                COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date AS dt_entrada,
                COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date AS dt_saida,
                mov.dat_alta::date AS dt_alta
            FROM movimentos mov
            JOIN t_clinica tc ON mov.isn_clinica = tc.isn_clinica
            JOIN t_enfermaria te ON mov.isn_enfermaria = te.isn_enfermaria
            JOIN t_leito tl ON mov.isn_leito = tl.isn_leito
            LEFT JOIN t_destino td ON mov.isn_destino = td.isn_destino
            WHERE mov.isn_clinica IN ({CLINICAS_TMP_PS_SQL})
              AND mov.isn_leito NOT IN ({LEITOS_TMP_PS_EXCL_SQL})
              AND COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date <= %s
              AND COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date >= %s
              {filt_sql}
        )
        SELECT
            COALESCE(dsc_destino, 'Destino ' || isn_destino::text) AS dsc_destino,
            COUNT(*)::numeric AS qtd
        FROM base
                WHERE {saida_valida_expr}
                    AND dt_alta BETWEEN %s AND %s
        GROUP BY dsc_destino, isn_destino
        ORDER BY isn_destino
    """, [data_fim, data_ini] + filt_vals + [data_ini, data_fim])

    porta_medico_row = fetch_one(f"""
        WITH primeira_consulta AS (
            SELECT
                tia.isn_internacao,
                MIN(
                    tia.dat_ini
                    + COALESCE(NULLIF(TRIM(tia.dsc_hor_ini), ''), '00:00')::time
                ) AS ts_consulta_med
            FROM t_internacao_atendimento tia
            WHERE tia.dat_ini IS NOT NULL
            GROUP BY tia.isn_internacao
        ),
        mov_inicio AS (
            SELECT DISTINCT ON (til.isn_internacao)
                til.isn_internacao,
                tc.dsc_clinica,
                te.dsc_enfermaria,
                tl.dsc_leito
            FROM t_internacao_leito til
            JOIN t_clinica tc ON til.isn_clinica = tc.isn_clinica
            JOIN t_enfermaria te ON til.isn_enfermaria = te.isn_enfermaria
            JOIN t_leito tl ON til.isn_leito = tl.isn_leito
            WHERE til.isn_clinica IN ({CLINICAS_FILTRO_SQL})
            ORDER BY
                til.isn_internacao,
                COALESCE(til.dat_internacao_info, til.dat_internacao),
                til.isn_internacao_leito
        )
        SELECT
            ROUND(
                AVG(EXTRACT(EPOCH FROM (pc.ts_consulta_med - ti.dat_cadastro)) / 60)::numeric,
                2
            ) AS valor
        FROM t_internacao ti
        JOIN mov_inicio mi ON mi.isn_internacao = ti.isn_internacao
        JOIN primeira_consulta pc ON pc.isn_internacao = ti.isn_internacao
        LEFT JOIN t_admissao ta ON ta.isn_internacao = ti.isn_internacao
        WHERE ti.isn_tipo_atendimento = 2
          AND ti.dat_cadastro::date BETWEEN %s AND %s
          AND pc.ts_consulta_med >= ti.dat_cadastro
          {filt_sql}
    """, [data_ini, data_fim] + filt_vals)

    internacoes_ps_row = fetch_one("""
        SELECT
            COUNT(*) AS total,
            ROUND(
                COUNT(*)::numeric
                / NULLIF((%s::date - %s::date + 1), 0)
            , 2) AS media_diaria
        FROM t_internacao ti
        WHERE ti.isn_tipo_atendimento = 2
          AND EXISTS (
              SELECT 1 FROM t_internacao_leito til
              WHERE til.isn_internacao = ti.isn_internacao
                AND til.isn_destino = 13
                AND COALESCE(til.dat_alta_info, til.dat_alta)::date BETWEEN %s AND %s
          )
    """, [data_fim, data_ini, data_ini, data_fim])

    _leitos_ps_filt_sql, _leitos_ps_filt_vals = _filtros_sql(clinica, enfermaria, leito, prefixo="AND")

    leitos_ativos_ps_row = fetch_one(f"""
        WITH
        dias AS (
            SELECT generate_series(%s::date, %s::date, INTERVAL '1 day')::date AS dt
        ),
        leitos_base_det AS (
            SELECT te.isn_clinica, tc.dsc_clinica, te.dsc_enfermaria, tl.dsc_leito, tl.isn_leito
            FROM t_leito tl
            JOIN t_enfermaria te ON te.isn_enfermaria = tl.isn_enfermaria
            JOIN t_clinica tc ON tc.isn_clinica = te.isn_clinica
            WHERE tl.flg_ativo = 'S'
              AND tl.flg_extra = 'N'
              AND te.flg_ativo = 'S'
              AND tc.flg_ativo = 'S'
              AND te.isn_clinica IN ({CLINICAS_PS_SQL})
              AND tl.isn_leito NOT IN ({LEITOS_TMP_PS_EXCL_SQL})
              {_leitos_ps_filt_sql}
        ),
        leitos_base AS (
            SELECT COUNT(*) AS total
            FROM leitos_base_det
        ),
        interdicoes_por_dia AS (
            SELECT
                d.dt,
                COUNT(DISTINCT lbd.isn_leito) AS interditados
            FROM dias d
            CROSS JOIN leitos_base_det lbd
            WHERE EXISTS (
                SELECT 1
                FROM t_leito_interditado tli
                WHERE tli.isn_leito = lbd.isn_leito
                  AND tli.dat_interdicao::date <= d.dt
                  AND (tli.dat_liberacao IS NULL OR tli.dat_liberacao::date > d.dt)
                  AND EXTRACT(EPOCH FROM (COALESCE(tli.dat_liberacao, CURRENT_TIMESTAMP) - tli.dat_interdicao)) > 86400
            )
            GROUP BY d.dt
        )
        SELECT CEIL(AVG(lb.total - COALESCE(ipd.interditados, 0))::numeric)::integer AS total
        FROM dias d
        CROSS JOIN leitos_base lb
        LEFT JOIN interdicoes_por_dia ipd ON ipd.dt = d.dt
    """, [data_ini, data_fim] + _leitos_ps_filt_vals)
    leitos_ativos_ps_total = leitos_ativos_ps_row["total"] if leitos_ativos_ps_row else None

    leitos_ativos_ps_detalhe = fetch_all(f"""
        WITH
        dias AS (
            SELECT generate_series(%s::date, %s::date, INTERVAL '1 day')::date AS dt
        ),
        leitos_base_det AS (
            SELECT te.isn_clinica, tc.dsc_clinica, te.dsc_enfermaria, tl.dsc_leito, tl.isn_leito
            FROM t_leito tl
            JOIN t_enfermaria te ON te.isn_enfermaria = tl.isn_enfermaria
            JOIN t_clinica tc ON tc.isn_clinica = te.isn_clinica
            WHERE tl.flg_ativo = 'S'
              AND tl.flg_extra = 'N'
              AND te.flg_ativo = 'S'
              AND tc.flg_ativo = 'S'
              AND te.isn_clinica IN ({CLINICAS_PS_SQL})
              AND tl.isn_leito NOT IN ({LEITOS_TMP_PS_EXCL_SQL})
              {_leitos_ps_filt_sql}
        ),
        leitos_por_clinica AS (
            SELECT isn_clinica, dsc_clinica, COUNT(*) AS total
            FROM leitos_base_det
            GROUP BY isn_clinica, dsc_clinica
        ),
        interdicoes_por_clinica_dia AS (
            SELECT
                d.dt,
                lbd.isn_clinica,
                COUNT(DISTINCT lbd.isn_leito) AS interditados
            FROM dias d
            CROSS JOIN leitos_base_det lbd
            WHERE EXISTS (
                SELECT 1 FROM t_leito_interditado tli
                WHERE tli.isn_leito = lbd.isn_leito
                  AND tli.dat_interdicao::date <= d.dt
                  AND (tli.dat_liberacao IS NULL OR tli.dat_liberacao::date > d.dt)
                  AND EXTRACT(EPOCH FROM (COALESCE(tli.dat_liberacao, CURRENT_TIMESTAMP) - tli.dat_interdicao)) > 86400
            )
            GROUP BY d.dt, lbd.isn_clinica
        )
        SELECT
            lpc.dsc_clinica,
            CEIL(AVG(lpc.total - COALESCE(ipcd.interditados, 0))::numeric)::integer AS qtd
        FROM leitos_por_clinica lpc
        CROSS JOIN dias d
        LEFT JOIN interdicoes_por_clinica_dia ipcd
            ON ipcd.isn_clinica = lpc.isn_clinica AND ipcd.dt = d.dt
        GROUP BY lpc.isn_clinica, lpc.dsc_clinica, lpc.total
        ORDER BY qtd DESC
    """, [data_ini, data_fim] + _leitos_ps_filt_vals)

    internacoes_detalhe_ps = fetch_all(_PRIMEIRA_CLINICA_CTE + f"""
        SELECT pc.dsc_clinica, COUNT(DISTINCT ti.isn_internacao)::integer AS qtd
        FROM t_internacao ti
        JOIN primeira_clinica pc ON pc.isn_internacao = ti.isn_internacao
        WHERE ti.isn_tipo_atendimento = 1
          AND ti.dat_cadastro::date BETWEEN %s AND %s
          AND pc.isn_clinica IN ({CLINICAS_PS_SQL})
        {filt_sql}
        GROUP BY pc.dsc_clinica
        ORDER BY qtd DESC
    """, [data_ini, data_fim] + filt_vals)

    saidas_val_ps = tmp_ps["saidas"] if tmp_ps and tmp_ps["saidas"] is not None else None
    giro_ps = round(float(saidas_val_ps) / leitos_ativos_ps_total, 2) if saidas_val_ps and leitos_ativos_ps_total else None

    return {
        "los_sem": los_sem["valor"] if los_sem else None,
        "los_com": los_com["valor"] if los_com else None,
        "porta_medico": porta_medico_row["valor"] if porta_medico_row else None,
        "paciente_dia_tmp_ps": tmp_ps["paciente_dia"] if tmp_ps else None,
        "saidas_tmp_ps": tmp_ps["saidas"] if tmp_ps else None,
        "saidas_tmp_ps_detalhe": [dict(r) for r in (saidas_tmp_ps_detalhe or [])],
        "tmp_ps": tmp_ps["valor"] if tmp_ps else None,
        "giro_ps": giro_ps,
        "internacoes_ps": internacoes_ps_row["total"] if internacoes_ps_row else None,
        "internacoes_ps_media": internacoes_ps_row["media_diaria"] if internacoes_ps_row else None,
        "internacoes_detalhe_ps": [dict(r) for r in (internacoes_detalhe_ps or [])],
        "leitos_ativos_ps": leitos_ativos_ps_total,
        "leitos_ativos_ps_detalhe": [dict(r) for r in (leitos_ativos_ps_detalhe or [])],
    }


# ---------------------------------------------------------------------------
# ATENDIMENTOS DE EMERGÊNCIA (por dia, por hora, por dia da semana e por
# classificação de risco). Atendimento de emergência = t_internacao com
# isn_tipo_atendimento = 2, datado por dat_cadastro (mesma base do LOS
# sem internação). Filtros opcionais: dias da semana e faixa de horário.
# ---------------------------------------------------------------------------

_DIAS_SEMANA = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]


def _horas_da_faixa(hora_ini, hora_fim):
    """Horas (0-23) da faixa, inclusive. Se hora_ini > hora_fim a faixa
    atravessa a meia-noite (ex.: 19h-7h)."""
    if hora_ini <= hora_fim:
        return list(range(hora_ini, hora_fim + 1))
    return list(range(hora_ini, 24)) + list(range(0, hora_fim + 1))


def get_atendimentos_emergencia(data_ini, data_fim, dias_semana=None, hora_ini=0, hora_fim=23):
    """dias_semana: lista de 0 (segunda) a 6 (domingo); vazio = todos."""
    dt_ini = date.fromisoformat(data_ini)
    dt_fim = date.fromisoformat(data_fim)
    if dt_ini > dt_fim:
        dt_ini, dt_fim = dt_fim, dt_ini
    dias_semana = sorted(set(dias_semana or range(7)))
    horas = _horas_da_faixa(hora_ini, hora_fim)

    atend_cte = """
        WITH atend AS (
            SELECT ti.isn_internacao, ti.dat_cadastro
            FROM t_internacao ti
            WHERE ti.isn_tipo_atendimento = 2
              AND ti.dat_cadastro::date BETWEEN %s AND %s
              AND EXTRACT(ISODOW FROM ti.dat_cadastro)::int - 1 = ANY(%s)
              AND EXTRACT(HOUR FROM ti.dat_cadastro)::int = ANY(%s)
        )
    """
    atend_vals = [dt_ini, dt_fim, dias_semana, horas]

    # Contagem por dia e hora do período inteiro, SEM os filtros de dia da
    # semana/horário: daqui saem as séries filtradas dos gráficos e também a
    # média/mediana, que são sempre do período todo (referência fixa).
    rows_dia_hora = fetch_all("""
        SELECT ti.dat_cadastro::date AS dia,
               EXTRACT(HOUR FROM ti.dat_cadastro)::int AS hora,
               COUNT(*) AS qtd
        FROM t_internacao ti
        WHERE ti.isn_tipo_atendimento = 2
          AND ti.dat_cadastro::date BETWEEN %s AND %s
        GROUP BY 1, 2
    """, [dt_ini, dt_fim])
    horas_set = set(horas)
    qtd_por_dia, qtd_por_hora = {}, {}
    base_por_dia = {}
    for r in rows_dia_hora or []:
        dia, hora, qtd = r["dia"], r["hora"], int(r["qtd"])
        base_por_dia[dia] = base_por_dia.get(dia, 0) + qtd
        if dia.weekday() in dias_semana and hora in horas_set:
            qtd_por_dia[dia] = qtd_por_dia.get(dia, 0) + qtd
            qtd_por_hora[hora] = qtd_por_hora.get(hora, 0) + qtd

    todos_dias = [dt_ini + timedelta(days=i) for i in range((dt_fim - dt_ini).days + 1)]
    base_dia = [base_por_dia.get(d, 0) for d in todos_dias]

    # Só os dias do período que caem nos dias da semana escolhidos.
    dias = [d for d in todos_dias if d.weekday() in dias_semana]
    por_dia = [qtd_por_dia.get(d, 0) for d in dias]

    # Dia da semana: total e média (o período pode ter quantidades
    # diferentes de cada dia da semana, então a média é por ocorrência do dia).
    totais_semana = [0] * 7
    ocorrencias_semana = [0] * 7
    for d, qtd in zip(dias, por_dia):
        totais_semana[d.weekday()] += qtd
        ocorrencias_semana[d.weekday()] += 1
    medias_semana = [
        round(t / o, 1) if o else None
        for t, o in zip(totais_semana, ocorrencias_semana)
    ]

    por_hora = [qtd_por_hora.get(h, 0) for h in horas]

    # Cada internação tem vários registros em t_internacao_risco (o primeiro,
    # da abertura da triagem, vem com isn_risco nulo; reclassificações geram
    # novos). Usa-se uma classificação por atendimento: a marcada com
    # flg_ultimo = 1 entre as que têm risco, senão a mais recente com risco.
    por_risco = fetch_all(atend_cte + """
        , risco_final AS (
            SELECT DISTINCT ON (tir.isn_internacao)
                tir.isn_internacao,
                tir.isn_risco
            FROM t_internacao_risco tir
            JOIN atend a ON a.isn_internacao = tir.isn_internacao
            WHERE tir.isn_risco IS NOT NULL
            ORDER BY tir.isn_internacao,
                     COALESCE(tir.flg_ultimo, 0) DESC,
                     tir.isn_internacao_risco DESC
        )
        SELECT rf.isn_risco, COUNT(*) AS qtd
        FROM atend a
        LEFT JOIN risco_final rf ON rf.isn_internacao = a.isn_internacao
        GROUP BY rf.isn_risco
    """, atend_vals)
    qtd_por_risco = {r["isn_risco"]: int(r["qtd"]) for r in (por_risco or [])}

    # Lista todas as classificações de t_risco (mesmo com zero) e, no fim,
    # "Sem classificação", para a tabela ter sempre as mesmas linhas.
    riscos = fetch_all("SELECT isn_risco, dsc_risco FROM t_risco ORDER BY isn_risco")
    linhas_risco = [(r["dsc_risco"], qtd_por_risco.get(r["isn_risco"], 0)) for r in (riscos or [])]
    linhas_risco.append(("Sem classificação", qtd_por_risco.get(None, 0)))

    total = sum(por_dia)
    qtd_dias = len(dias)

    return {
        "total": total,
        # média/mediana do período inteiro, sem filtro de dia da semana/horário
        "media_diaria": round(sum(base_dia) / len(base_dia), 1) if base_dia else None,
        "mediana_diaria": round(statistics.median(base_dia), 1) if base_dia else None,
        "dias": [d.strftime("%d/%m/%Y") for d in dias],
        "por_dia": por_dia,
        "horas": [f"{h:02d}h" for h in horas],
        "por_hora": por_hora,
        "media_hora": [round(q / qtd_dias, 1) if qtd_dias else None for q in por_hora],
        "dias_semana": _DIAS_SEMANA,
        "por_dia_semana": totais_semana,
        "media_dia_semana": medias_semana,
        "por_risco": [
            {
                "dsc_risco": dsc,
                "qtd": qtd,
                "percentual": round(qtd / total * 100, 1) if total else None,
            }
            for dsc, qtd in linhas_risco
        ],
    }

# ---------------------------------------------------------------------------
# PÁGINA INDICADORES LEAN (/lean) — visão consolidada hospitalar + emergência
# (antes servida por um app separado na porta 5200)
# ---------------------------------------------------------------------------

def get_indicadores(data_ini, data_fim, clinica, enfermaria, leito):
    """Snapshot de um período com todos os indicadores (hospitalares + emergência)."""
    return {
        **get_indicadores_hospitalar(data_ini, data_fim, clinica, enfermaria, leito),
        **get_indicadores_emergencia(data_ini, data_fim, clinica, enfermaria, leito),
    }


def get_indicadores_mensais(clinica, enfermaria, leito, mes_ini=None, mes_fim=None):
    """Tendência mensal com todos os indicadores (hospitalares + emergência)."""
    return {
        **get_mensal_hospitalar(clinica, enfermaria, leito, mes_ini, mes_fim),
        **get_mensal_emergencia(clinica, enfermaria, leito, mes_ini, mes_fim),
    }


# ---------------------------------------------------------------------------
# AUDITORIA (/lean/auditoria) — registros que compõem cada indicador
# ---------------------------------------------------------------------------

def get_auditoria_los_com(data_ini, data_fim):
    rows = fetch_all(f"""
        WITH
        internacao_datas AS (
            SELECT
                ti.isn_internacao,
                tp.num_prontuario,
                MIN(til.dat_internacao) AS dat_entrada,
                MAX(til.dat_alta)       AS dat_saida
            FROM t_internacao ti
            JOIN t_paciente tp ON tp.isn_paciente = ti.isn_paciente
            JOIN t_internacao_leito til ON til.isn_internacao = ti.isn_internacao
            WHERE til.dat_internacao IS NOT NULL
            GROUP BY ti.isn_internacao, tp.num_prontuario
        ),
        continuidade AS (
            SELECT
                isn_internacao,
                num_prontuario,
                dat_entrada,
                dat_saida,
                CASE
                    WHEN LAG(dat_saida) OVER (
                        PARTITION BY num_prontuario ORDER BY dat_entrada, isn_internacao
                    ) = dat_entrada
                    THEN 0 ELSE 1
                END AS novo_grupo
            FROM internacao_datas
        ),
        grupos AS (
            SELECT
                isn_internacao,
                num_prontuario,
                dat_entrada,
                SUM(novo_grupo) OVER (
                    PARTITION BY num_prontuario ORDER BY dat_entrada, isn_internacao
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS grp_num
            FROM continuidade
        ),
        cadeia AS (
            SELECT
                g.isn_internacao,
                FIRST_VALUE(g.isn_internacao) OVER (
                    PARTITION BY g.num_prontuario, g.grp_num
                    ORDER BY g.dat_entrada, g.isn_internacao
                ) AS id_grupo
            FROM grupos g
        ),
        base AS (
            SELECT
                c.id_grupo,
                til.isn_clinica,
                tc.dsc_clinica,
                COALESCE(
                    til.dat_internacao_info + til.hor_internacao_info::time,
                    til.dat_internacao + til.hor_internacao::time
                ) AS ts_internacao,
                COALESCE(
                    til.dat_alta_info + til.hor_alta_info::time,
                    til.dat_alta + til.hor_alta::time
                ) AS ts_alta
            FROM cadeia c
            JOIN t_internacao_leito til ON til.isn_internacao = c.isn_internacao
            JOIN t_clinica tc ON tc.isn_clinica = til.isn_clinica
            WHERE til.dat_internacao IS NOT NULL
        ),
        grupo_primeiro AS (
            SELECT id_grupo, MIN(ts_internacao) AS ts_primeiro_grupo
            FROM base
            GROUP BY id_grupo
        ),
        grupo_inicio AS (
            SELECT
                b.id_grupo,
                MIN(b.ts_internacao) AS ts_clinica_1,
                gp.ts_primeiro_grupo,
                ti.dat_cadastro
            FROM base b
            JOIN grupo_primeiro gp ON gp.id_grupo = b.id_grupo
            JOIN t_internacao ti ON ti.isn_internacao = b.id_grupo
            JOIN (
                SELECT DISTINCT ON (isn_internacao) isn_internacao, isn_destino
                FROM t_internacao_leito
                ORDER BY isn_internacao,
                    COALESCE(dat_alta_info + hor_alta_info::time, dat_alta + hor_alta::time) DESC NULLS LAST,
                    isn_internacao_leito DESC
            ) til_ult ON til_ult.isn_internacao = b.id_grupo
            WHERE b.isn_clinica = 1
              AND ti.isn_tipo_atendimento = 2
              AND til_ult.isn_destino = 13
              AND ti.dat_cadastro::date BETWEEN %s AND %s
            GROUP BY b.id_grupo, gp.ts_primeiro_grupo, ti.dat_cadastro
        ),
        grupo_destino AS (
            SELECT
                b.id_grupo,
                MIN(b.ts_internacao) AS ts_primeira_clinica,
                MIN(b.dsc_clinica)   AS dsc_clinica_destino
            FROM base b
            JOIN grupo_inicio g ON g.id_grupo = b.id_grupo
            WHERE b.isn_clinica IN ({CLINICAS_LOS_COM_DEST_SQL})
              AND b.ts_internacao IS NOT NULL
              AND b.ts_internacao >= g.ts_clinica_1
            GROUP BY b.id_grupo
        ),
        grupo_fallback AS (
            SELECT
                b.id_grupo,
                MAX(COALESCE(b.ts_alta, CURRENT_TIMESTAMP)) AS ts_ultima_alta,
                BOOL_OR(b.ts_alta IS NULL) AS tem_aberto
            FROM base b
            JOIN grupo_inicio g ON g.id_grupo = b.id_grupo
            WHERE b.ts_internacao >= g.ts_clinica_1
            GROUP BY b.id_grupo
        ),
        grupo_final AS (
            SELECT
                g.id_grupo,
                g.dat_cadastro,
                g.ts_clinica_1,
                g.ts_primeiro_grupo,
                d.ts_primeira_clinica,
                d.dsc_clinica_destino,
                f.ts_ultima_alta,
                f.tem_aberto,
                COALESCE(d.ts_primeira_clinica, f.ts_ultima_alta) AS ts_referencia,
                CASE
                    WHEN d.ts_primeira_clinica IS NOT NULL THEN 'Destino'
                    WHEN f.tem_aberto THEN 'Em aberto (hoje)'
                    ELSE 'Fallback (última alta)'
                END AS tipo_referencia
            FROM grupo_inicio g
            LEFT JOIN grupo_destino d ON d.id_grupo = g.id_grupo
            LEFT JOIN grupo_fallback f ON f.id_grupo = g.id_grupo
        )
        SELECT
            gf.id_grupo,
            gf.dat_cadastro::text                              AS dat_cadastro,
            gf.ts_clinica_1::text                             AS ts_clinica_1,
            gf.ts_primeiro_grupo::text                        AS ts_primeiro_grupo,
            gf.ts_referencia::text                            AS ts_referencia,
            gf.dsc_clinica_destino,
            gf.tipo_referencia,
            gf.tem_aberto,
            ROUND(
                EXTRACT(EPOCH FROM (gf.ts_referencia - gf.ts_clinica_1)) / 60
            )::integer                                        AS los_min,
            ROUND(
                EXTRACT(EPOCH FROM (gf.ts_referencia - gf.ts_clinica_1)) / 3600.0, 1
            )::numeric                                        AS los_horas
        FROM grupo_final gf
        WHERE gf.ts_referencia > gf.ts_clinica_1
        ORDER BY gf.ts_primeiro_grupo DESC
    """, [data_ini, data_fim])

    return [dict(r) for r in (rows or [])]


def get_auditoria_los_sem(data_ini, data_fim):
    """Auditoria LOS Sem Internação — cada atendimento PS com alta direta (sem internação)."""
    rows = fetch_all("""
        SELECT
            ti.isn_internacao,
            tp.num_prontuario,
            ti.dat_cadastro,
            COALESCE(til.dat_internacao_info + til.hor_internacao_info::time,
                     til.dat_internacao      + til.hor_internacao::time)     AS ts_entrada,
            COALESCE(til.dat_alta_info + til.hor_alta_info::time,
                     til.dat_alta      + til.hor_alta::time)                 AS ts_saida,
            ROUND(EXTRACT(EPOCH FROM (
                COALESCE(til.dat_alta_info + til.hor_alta_info::time, til.dat_alta + til.hor_alta::time)
                -
                COALESCE(til.dat_internacao_info + til.hor_internacao_info::time, til.dat_internacao + til.hor_internacao::time)
            )) / 60::numeric, 1) AS los_min,
            ROUND(EXTRACT(EPOCH FROM (
                COALESCE(til.dat_alta_info + til.hor_alta_info::time, til.dat_alta + til.hor_alta::time)
                -
                COALESCE(til.dat_internacao_info + til.hor_internacao_info::time, til.dat_internacao + til.hor_internacao::time)
            )) / 3600::numeric, 2) AS los_horas,
            td.dsc_destino
        FROM t_internacao ti
        JOIN t_paciente tp ON tp.isn_paciente = ti.isn_paciente
        JOIN t_internacao_leito til ON til.isn_internacao = ti.isn_internacao
        JOIN t_destino td ON td.isn_destino = til.isn_destino
        WHERE ti.isn_tipo_atendimento = 2
          AND til.isn_destino IN (1, 3, 4)
          AND til.dat_alta IS NOT NULL
          AND ti.dat_cadastro::date BETWEEN %s AND %s
        ORDER BY ti.dat_cadastro DESC
    """, [data_ini, data_fim])
    return [dict(r) for r in (rows or [])]


def get_auditoria_porta_medico(data_ini, data_fim):
    """Auditoria Tempo Porta-Médico — cada atendimento PS com seu tempo de espera até consulta."""
    rows = fetch_all(f"""
        WITH primeira_consulta AS (
            SELECT
                tia.isn_internacao,
                MIN(
                    tia.dat_ini
                    + COALESCE(NULLIF(TRIM(tia.dsc_hor_ini), ''), '00:00')::time
                ) AS ts_consulta_med
            FROM t_internacao_atendimento tia
            WHERE tia.dat_ini IS NOT NULL
            GROUP BY tia.isn_internacao
        ),
        mov_inicio AS (
            SELECT DISTINCT ON (til.isn_internacao)
                til.isn_internacao,
                tc.dsc_clinica,
                te.dsc_enfermaria,
                tl.dsc_leito
            FROM t_internacao_leito til
            JOIN t_clinica tc ON til.isn_clinica = tc.isn_clinica
            JOIN t_enfermaria te ON til.isn_enfermaria = te.isn_enfermaria
            JOIN t_leito tl ON til.isn_leito = tl.isn_leito
            WHERE til.isn_clinica IN ({CLINICAS_FILTRO_SQL})
            ORDER BY
                til.isn_internacao,
                COALESCE(til.dat_internacao_info, til.dat_internacao),
                til.isn_internacao_leito
        )
        SELECT
            ti.isn_internacao,
            tp.num_prontuario,
            ti.dat_cadastro,
            pc.ts_consulta_med,
            ROUND(
                EXTRACT(EPOCH FROM (pc.ts_consulta_med - ti.dat_cadastro)) / 60::numeric, 1
            ) AS tempo_min,
            ROUND(
                EXTRACT(EPOCH FROM (pc.ts_consulta_med - ti.dat_cadastro)) / 3600::numeric, 2
            ) AS tempo_horas,
            mi.dsc_clinica,
            mi.dsc_enfermaria,
            mi.dsc_leito
        FROM t_internacao ti
        JOIN t_paciente tp ON tp.isn_paciente = ti.isn_paciente
        JOIN mov_inicio mi ON mi.isn_internacao = ti.isn_internacao
        JOIN primeira_consulta pc ON pc.isn_internacao = ti.isn_internacao
        WHERE ti.isn_tipo_atendimento = 2
          AND ti.dat_cadastro::date BETWEEN %s AND %s
          AND pc.ts_consulta_med >= ti.dat_cadastro
        ORDER BY ti.dat_cadastro DESC
    """, [data_ini, data_fim])
    return [dict(r) for r in (rows or [])]


def get_auditoria_internacoes_ps(data_ini, data_fim):
    """Auditoria Internações PS — cada internação proveniente do PS (isn_destino=13)."""
    rows = fetch_all("""
        SELECT
            ti.isn_internacao,
            tp.num_prontuario,
            ti.dat_cadastro,
            COALESCE(
                til_ent.dat_internacao_info + til_ent.hor_internacao_info::time,
                til_ent.dat_internacao      + til_ent.hor_internacao::time
            ) AS ts_entrada,
            COALESCE(
                til_ult.dat_alta_info + til_ult.hor_alta_info::time,
                til_ult.dat_alta      + til_ult.hor_alta::time
            ) AS ts_alta,
            tc_ent.dsc_clinica  AS clinica_entrada,
            tc_ult.dsc_clinica  AS clinica_alta,
            td_ult.dsc_destino
        FROM t_internacao ti
        JOIN t_paciente tp ON tp.isn_paciente = ti.isn_paciente
        -- primeira movimentação (entrada)
        JOIN LATERAL (
            SELECT * FROM t_internacao_leito til2
            WHERE til2.isn_internacao = ti.isn_internacao
            ORDER BY COALESCE(til2.dat_internacao_info, til2.dat_internacao),
                     til2.isn_internacao_leito
            LIMIT 1
        ) til_ent ON TRUE
        -- última movimentação (alta)
        JOIN LATERAL (
            SELECT * FROM t_internacao_leito til3
            WHERE til3.isn_internacao = ti.isn_internacao
            ORDER BY COALESCE(til3.dat_alta_info, til3.dat_alta) DESC NULLS LAST,
                     til3.isn_internacao_leito DESC
            LIMIT 1
        ) til_ult ON TRUE
        JOIN t_clinica tc_ent ON tc_ent.isn_clinica = til_ent.isn_clinica
        JOIN t_clinica tc_ult ON tc_ult.isn_clinica = til_ult.isn_clinica
        LEFT JOIN t_destino td_ult ON td_ult.isn_destino = til_ult.isn_destino
                WHERE ti.isn_tipo_atendimento = 2
                    AND EXISTS (
                            SELECT 1 FROM t_internacao_leito til
                            WHERE til.isn_internacao = ti.isn_internacao
                                AND til.isn_destino = 13
                                AND COALESCE(til.dat_alta_info, til.dat_alta)::date BETWEEN %s AND %s
                    )
        ORDER BY ti.dat_cadastro DESC
    """, [data_ini, data_fim])
    return [dict(r) for r in (rows or [])]


def get_auditoria_tmph(data_ini, data_fim):
    """Auditoria TMPH — movimentos de internação que compõem paciente-dia e saídas hospitalares."""
    rows = fetch_all(f"""
        WITH movimentos AS (
            SELECT
                til.*,
                LEAD(til.isn_clinica) OVER (
                    PARTITION BY til.isn_internacao
                    ORDER BY COALESCE(til.dat_internacao_info, til.dat_internacao), til.isn_internacao_leito
                ) AS prox_clinica
            FROM t_internacao_leito til
        )
        SELECT
            mov.isn_internacao_leito,
            mov.isn_internacao,
            tp.num_prontuario,
            tc.dsc_clinica,
            te.dsc_enfermaria,
            tl.dsc_leito,
            COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date AS dt_entrada,
            COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date AS dt_saida,
            mov.dat_alta::date AS dt_alta,
            td.dsc_destino,
            CASE
                WHEN (COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date
                      - COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date) > 0
                THEN (COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date
                      - COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date)
                WHEN (COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date
                      = COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date)
                     AND mov.dat_alta IS NOT NULL AND COALESCE(mov.isn_destino, 0) <> 1
                THEN 1
                ELSE 0
            END AS paciente_dia,
            CASE
                WHEN mov.isn_destino IN (1, 3, 4)
                     AND mov.dat_alta::date BETWEEN %s AND %s
                THEN 1 ELSE 0
            END AS saida_valida
        FROM movimentos mov
        JOIN t_internacao ti ON ti.isn_internacao = mov.isn_internacao
        JOIN t_paciente tp ON tp.isn_paciente = ti.isn_paciente
        JOIN t_clinica tc ON mov.isn_clinica = tc.isn_clinica
        JOIN t_enfermaria te ON mov.isn_enfermaria = te.isn_enfermaria
        JOIN t_leito tl ON mov.isn_leito = tl.isn_leito
        LEFT JOIN t_destino td ON td.isn_destino = mov.isn_destino
        WHERE mov.isn_clinica IN ({CLINICAS_TMPH_SQL})
          AND COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date <= %s
          AND COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date >= %s
        ORDER BY COALESCE(mov.dat_internacao_info, mov.dat_internacao) DESC
    """, [data_ini, data_fim, data_fim, data_ini])
    return [dict(r) for r in (rows or [])]


def get_auditoria_tmp_ps(data_ini, data_fim):
    """Auditoria TMP PS — movimentos de internação nas clínicas do PS."""
    rows = fetch_all(f"""
        WITH movimentos AS (
            SELECT
                til.*,
                LEAD(til.isn_clinica) OVER (
                    PARTITION BY til.isn_internacao
                    ORDER BY COALESCE(til.dat_internacao_info, til.dat_internacao), til.isn_internacao_leito
                ) AS prox_clinica
            FROM t_internacao_leito til
        )
        SELECT
            mov.isn_internacao_leito,
            mov.isn_internacao,
            tp.num_prontuario,
            tc.dsc_clinica,
            te.dsc_enfermaria,
            tl.dsc_leito,
            COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date AS dt_entrada,
            COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date AS dt_saida,
            mov.dat_alta::date AS dt_alta,
            td.dsc_destino,
            CASE
                WHEN (COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date
                      - COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date) > 0
                THEN (COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date
                      - COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date)
                WHEN (COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date
                      = COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date)
                     AND mov.dat_alta IS NOT NULL AND COALESCE(mov.isn_destino, 0) <> 1
                THEN 1
                ELSE 0
            END AS paciente_dia,
            CASE
                WHEN mov.isn_destino IN (1, 3, 4)
                     AND mov.dat_alta::date BETWEEN %s AND %s
                THEN 1 ELSE 0
            END AS saida_valida
        FROM movimentos mov
        JOIN t_internacao ti ON ti.isn_internacao = mov.isn_internacao
        JOIN t_paciente tp ON tp.isn_paciente = ti.isn_paciente
        JOIN t_clinica tc ON mov.isn_clinica = tc.isn_clinica
        JOIN t_enfermaria te ON mov.isn_enfermaria = te.isn_enfermaria
        JOIN t_leito tl ON mov.isn_leito = tl.isn_leito
        LEFT JOIN t_destino td ON td.isn_destino = mov.isn_destino
        WHERE mov.isn_clinica IN ({CLINICAS_TMP_PS_SQL})
          AND mov.isn_leito NOT IN ({LEITOS_TMP_PS_EXCL_SQL})
          AND COALESCE(mov.dat_internacao_info, mov.dat_internacao)::date <= %s
          AND COALESCE(mov.dat_alta_info, mov.dat_alta, CURRENT_DATE)::date >= %s
        ORDER BY COALESCE(mov.dat_internacao_info, mov.dat_internacao) DESC
    """, [data_ini, data_fim, data_fim, data_ini])
    return [dict(r) for r in (rows or [])]
