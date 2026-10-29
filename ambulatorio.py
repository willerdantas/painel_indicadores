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
t_internacao e t_marcacao são lidas uma única vez por período
(_visitas_periodo, um registro por atendimento, em memória por alguns
minutos); o resumo por dia, as consultas e as salas partem desses registros,
e os filtros de especialidade, status, sexo, município e macrorregião são
aplicados neles, sem nova consulta ao banco. Pedidos simultâneos do mesmo
período esperam a mesma consulta (_em_cache). Consulta acima de 5 s deixa
um aviso no log ("[ambulatorio] consulta ... levou").

Horário e tempo das consultas: vêm do registro do atendimento médico
(t_internacao_atendimento), com a data (dat_ini/dat_fim) e a hora em texto
"HH:MM" (dsc_hor_ini/dsc_hor_fim) de início e de fim. Só a consulta realizada
tem esse registro (falta e cancelamento não). A tabela não tem índice em
dat_ini nem em isn_internacao (leitura inteira, ~1 s). A especialidade de
cada consulta vem dos atendimentos já lidos; não buscar a marcação consulta
a consulta no banco: sem o índice em t_marcacao.isn_internacao, isso lia a
tabela inteira uma vez por consulta (minutos).
"""

import statistics
import threading
import time
from datetime import date, timedelta

import banco_painel
from indicadores_hcasg import fetch_all, _DIAS_SEMANA, _meses_iso, _meses_labels

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


_travas = {}  # uma trava por chave em cálculo


def _em_cache(chave, calcular):
    """Valor do cache ou calcular(). Pedidos simultâneos da mesma chave (a
    página e as salas, ou filtros trocados enquanto o banco responde) esperam
    o primeiro cálculo em vez de repetir a mesma consulta pesada no banco."""
    valor = _do_cache(chave)
    if valor is not None:
        return valor
    with _cache_lock:
        trava = _travas.setdefault(chave, threading.Lock())
    with trava:
        valor = _do_cache(chave)
        if valor is None:
            valor = _guardar(chave, calcular())
        with _cache_lock:
            _travas.pop(chave, None)
    return valor


_AVISO_LENTO = 5  # segundos


def _consultar(nome, sql, params):
    """fetch_all com aviso no log quando a consulta demora (diagnóstico no servidor)."""
    inicio = time.monotonic()
    rows = fetch_all(sql, params) or []
    duracao = time.monotonic() - inicio
    if duracao > _AVISO_LENTO:
        print(f'[ambulatorio] consulta {nome} levou {duracao:.1f} s ({len(rows)} linhas); '
              'confira os índices citados no início de ambulatorio.py')
    return rows


# marcação de cada atendimento de "atend" (especialidade e status)
_CTE_MARCACAO = """
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
# município do atendimento (com a UF: há nomes iguais em estados diferentes) e
# sexo do paciente; "a" precisa ter isn_paciente, isn_municipio e isn_uf
_MUNICIPIO_SEXO = """
            COALESCE(NULLIF(TRIM(tmu.dsc_municipio), '') || COALESCE('/' || NULLIF(TRIM(tuf.sgl_uf), ''), ''),
                     'Sem município') AS municipio,
            COALESCE(NULLIF(TRIM(tp.dsc_sexo), ''), 'Não informado') AS sexo"""
_JOINS_MUNICIPIO_SEXO = """
        LEFT JOIN t_municipio tmu ON tmu.isn_municipio = a.isn_municipio
        LEFT JOIN t_uf tuf ON tuf.isn_uf = a.isn_uf
        LEFT JOIN t_paciente tp ON tp.isn_paciente = a.isn_paciente"""
# atendimentos do período e a marcação de cada um
_ATEND_PERIODO = """
            SELECT ti.isn_internacao, ti.dat_cadastro, ti.isn_paciente, ti.isn_municipio, ti.isn_uf
            FROM t_internacao ti
            WHERE ti.isn_tipo_atendimento = 4
              -- intervalo na própria coluna (sem ::date) para poder usar índice em dat_cadastro
              AND ti.dat_cadastro >= %s AND ti.dat_cadastro < %s"""
_CTE_ATENDIMENTOS = f"""
        WITH atend AS MATERIALIZED ({_ATEND_PERIODO}
        ),{_CTE_MARCACAO}"""


def _resumo_periodo(dt_ini, dt_fim):
    """Atendimentos agrupados por dia × especialidade × status × município ×
    sexo, montados dos atendimentos do período (sem outra consulta ao banco)."""
    grupos = {}
    for _, esp, st, dia, municipio, sexo, *_ in _visitas_periodo(dt_ini, dt_fim):
        chave = (dia, esp, st, municipio, sexo)
        grupos[chave] = grupos.get(chave, 0) + 1
    return [{"dia": d, "especialidade": e, "status": s, "municipio": m, "sexo": x, "qtd": q}
            for (d, e, s, m, x), q in grupos.items()]


_HORA_VALIDA = "'^([01]?[0-9]|2[0-3]):[0-5][0-9]$'"
_MINUTOS_MAXIMO = 12 * 60  # consulta mais longa que isso é erro de digitação da hora
_SEM_PERFIL = ('Sem especialidade', 'Sem marcação', 'Não informado', 'Sem município')


def _perfil_atendimentos(internacoes):
    """{isn_internacao: (especialidade, status, sexo, município)} de
    atendimentos avulsos (consultas cujo atendimento foi cadastrado fora do
    período). Uma leitura de t_marcacao para todos, não uma por atendimento."""
    rows = _consultar('perfil', f"""
        WITH atend AS MATERIALIZED (
            SELECT ti.isn_internacao, ti.isn_paciente, ti.isn_municipio, ti.isn_uf
            FROM t_internacao ti
            WHERE ti.isn_internacao = ANY(%s)
        ),{_CTE_MARCACAO}
        SELECT a.isn_internacao AS internacao,{_ESPECIALIDADE_STATUS}{_MUNICIPIO_SEXO}
        FROM atend a{_JOINS_ESPECIALIDADE_STATUS}{_JOINS_MUNICIPIO_SEXO}
    """, [list(internacoes)])
    return {r["internacao"]: (r["especialidade"].strip(), r["status"].strip(), r["sexo"].strip(),
                              r["municipio"].strip()) for r in rows}


def _consultas_periodo(dt_ini, dt_fim):
    """Um registro por consulta realizada no período (data de início em
    t_internacao_atendimento.dat_ini): (especialidade, status, hora de início,
    duração em minutos, sexo, município, dia de início). Hora fora do formato HH:MM, sem fim
    ou duração fora de 0 a 12 h: None (com cache em memória).

    Especialidade, status, sexo e município vêm dos atendimentos do período
    (_visitas_periodo, já em memória); só a consulta de atendimento
    cadastrado noutro dia vai ao banco. Assim a busca da marcação não depende
    de índice em t_marcacao.isn_internacao (sem ele, buscar a marcação de
    cada consulta lia a tabela inteira uma vez por consulta)."""
    def calcular():
        rows = _consultar('consultas', f"""
            SELECT x.isn_internacao AS internacao, x.dat_ini::date AS dia,
                CASE WHEN x.dsc_hor_ini ~ {_HORA_VALIDA}
                     THEN split_part(x.dsc_hor_ini, ':', 1)::int END AS hora,
                CASE WHEN x.dsc_hor_ini ~ {_HORA_VALIDA} AND x.dsc_hor_fim ~ {_HORA_VALIDA} AND x.dat_fim IS NOT NULL
                     THEN EXTRACT(EPOCH FROM (x.dat_fim::date + x.dsc_hor_fim::time)
                                           - (x.dat_ini::date + x.dsc_hor_ini::time)) / 60 END AS minutos
            FROM t_internacao_atendimento x
            JOIN t_internacao ti ON ti.isn_internacao = x.isn_internacao AND ti.isn_tipo_atendimento = 4
            WHERE x.dat_ini >= %s AND x.dat_ini < %s
        """, [dt_ini, dt_fim + timedelta(days=1)])
        perfil = {v[7]: (v[1], v[2], v[5], v[4]) for v in _visitas_periodo(dt_ini, dt_fim)}
        faltam = {r["internacao"] for r in rows} - perfil.keys()
        if faltam:
            perfil.update(_perfil_atendimentos(sorted(faltam)))
        consultas = []
        for r in rows:
            esp, st, sexo, municipio = perfil.get(r["internacao"], _SEM_PERFIL)
            minutos = r["minutos"]
            consultas.append((esp, st, r["hora"],
                              float(minutos) if minutos is not None and 0 <= minutos <= _MINUTOS_MAXIMO else None,
                              sexo, municipio, r["dia"]))
        return consultas
    return _em_cache(('consultas', dt_ini, dt_fim), calcular)


def tempo_consulta(consultas, especialidade=None, status=None):
    """Tempo médio e mediano (em minutos) por consulta, por especialidade (da
    mais demorada para a mais rápida), e o total. Só entram as consultas com
    início e fim válidos."""
    tempos = {}
    for esp, st, _, minutos, *_ in consultas:
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
    município, sexo, idade, isn_internacao). É a única leitura de
    t_internacao e t_marcacao da página: o resumo, as consultas e as salas
    partem daqui (com cache em memória). O município é o do atendimento
    (t_internacao.isn_municipio), com a UF: há nomes iguais em estados diferentes.
    O sexo vem do cadastro do paciente (t_paciente.dsc_sexo); a idade, em anos
    completos na data do atendimento, de t_paciente.dat_nascimento (sem data
    ou fora de 0 a 120 anos: None)."""
    def calcular():
        rows = _consultar('atendimentos', f"""{_CTE_ATENDIMENTOS}
            SELECT a.isn_internacao AS internacao, a.isn_paciente AS paciente,{_ESPECIALIDADE_STATUS}
                a.dat_cadastro::date AS dia,{_MUNICIPIO_SEXO},
                date_part('year', age(a.dat_cadastro::date, tp.dat_nascimento::date))::int AS idade
            FROM atend a{_JOINS_ESPECIALIDADE_STATUS}{_JOINS_MUNICIPIO_SEXO}
        """, [dt_ini, dt_fim + timedelta(days=1)])
        return [
            (r["paciente"], r["especialidade"].strip(), r["status"].strip(), r["dia"], r["municipio"].strip(),
             r["sexo"].strip(), r["idade"] if r["idade"] is not None and 0 <= r["idade"] <= _IDADE_MAXIMA else None,
             r["internacao"])
            for r in rows
        ]
    return _em_cache(('visitas', dt_ini, dt_fim), calcular)


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


def macrorregiao_do_municipio(municipio, macro_do_municipio):
    """Macrorregião de saúde de um município "NOME/UF" (cadastro {município
    sem acento: macrorregião}): do Ceará fora do cadastro, "Ceará (não
    classificado)"; de outro estado, "Outros estados"; "Sem município" fica igual.
    Sem a UF (atendimentos antigos têm só o nome): pelo cadastro do Ceará ou,
    fora dele, "UF não informada"."""
    if municipio == 'Sem município':
        return municipio
    nome, _, uf = municipio.rpartition('/')
    if not nome:
        return macro_do_municipio.get(banco_painel._chave_nome(municipio)) or 'UF não informada'
    if uf != 'CE':
        return 'Outros estados'
    return macro_do_municipio.get(banco_painel._chave_nome(nome)) or 'Ceará (não classificado)'


def filtro_paciente(sexo=None, municipio=None, macrorregiao=None, macro_do_municipio=None):
    """Função (sexo, município) -> se o atendimento passa nos filtros de sexo,
    município e macrorregião; None quando nenhum dos três está ativo."""
    if not (sexo or municipio or macrorregiao):
        return None
    return lambda s, m: ((not sexo or s == sexo) and (not municipio or m == municipio)
                         and (not macrorregiao or macrorregiao_do_municipio(m, macro_do_municipio or {}) == macrorregiao))


def por_macrorregiao(visitas, macro_do_municipio, especialidade=None, status=None):
    """Atendimentos e prontuários únicos por macrorregião de saúde do Ceará
    (cadastro Regiões de Saúde: {município sem acento: macrorregião}).
    Município do Ceará fora do cadastro: "Ceará (não classificado)"; de outro
    estado: "Outros estados"."""
    atend, pacientes = {}, {}
    for paciente, esp, st, _, municipio, *_ in visitas:
        if (especialidade and esp != especialidade) or (status and st != status):
            continue
        grupo = macrorregiao_do_municipio(municipio, macro_do_municipio)
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
    é separada da principal e a página a carrega depois dos outros gráficos.
    Do banco vem só a sala de cada atendimento; especialidade, status, sexo e
    município vêm de _visitas_periodo (sem ler t_marcacao de novo)."""
    def calcular():
        rows = _consultar('salas', f"""
            WITH atend AS MATERIALIZED ({_ATEND_PERIODO}
            ),
            leito AS MATERIALIZED (
                -- um leito por atendimento (o último, se houver mais)
                SELECT DISTINCT ON (il.isn_internacao) il.isn_internacao, il.isn_leito
                FROM t_internacao_leito il
                WHERE il.isn_internacao IN (SELECT isn_internacao FROM atend)
                ORDER BY il.isn_internacao, il.isn_internacao_leito DESC
            )
            SELECT l.isn_internacao AS internacao, NULLIF(TRIM(tl.dsc_leito), '') AS sala
            FROM leito l
            LEFT JOIN t_leito tl ON tl.isn_leito = l.isn_leito
        """, [dt_ini, dt_fim + timedelta(days=1)])
        sala_de = {r["internacao"]: r["sala"] for r in rows}
        grupos = {}
        for _, esp, st, _, municipio, sexo, _, internacao in _visitas_periodo(dt_ini, dt_fim):
            chave = ((sala_de.get(internacao) or 'Sem sala').strip(), esp, st, municipio, sexo)
            grupos[chave] = grupos.get(chave, 0) + 1
        return [{"sala": s, "especialidade": e, "status": st, "municipio": m, "sexo": x, "qtd": q}
                for (s, e, st, m, x), q in grupos.items()]
    return _em_cache(('salas', dt_ini, dt_fim), calcular)


def get_salas(data_ini, data_fim, especialidade=None, status=None, sexo=None, municipio=None, macrorregiao=None):
    """Consultas por status em cada sala: salas da mais para a menos usada e
    uma série por status (mesmos filtros da página)."""
    dt_ini, dt_fim = sorted((date.fromisoformat(data_ini), date.fromisoformat(data_fim)))
    filtro = filtro_paciente(sexo, municipio, macrorregiao,
                             banco_painel.macrorregiao_dos_municipios() if macrorregiao else None)
    linhas = [r for r in _salas_periodo(dt_ini, dt_fim)
              if (not especialidade or r["especialidade"] == especialidade)
              and (not status or r["status"] == status)
              and (filtro is None or filtro(r["sexo"], r["municipio"]))]
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


def get_ambulatorio(data_ini, data_fim, especialidade=None, status=None,
                    sexo=None, municipio=None, macrorregiao=None):
    dt_ini = date.fromisoformat(data_ini)
    dt_fim = date.fromisoformat(data_fim)
    if dt_ini > dt_fim:
        dt_ini, dt_fim = dt_fim, dt_ini

    resumo = _resumo_periodo(dt_ini, dt_fim)
    macro_do_municipio = banco_painel.macrorregiao_dos_municipios()

    # opções dos filtros: tudo o que aparece no período (sem filtro aplicado)
    municipios = {r["municipio"] for r in resumo}
    opcoes = {
        "especialidades": sorted({r["especialidade"] for r in resumo}),
        "status": [x["nome"] for x in _contar(resumo, "status")],
        "sexos": [x["nome"] for x in _contar(resumo, "sexo")],
        "municipios": sorted(municipios),
        "macrorregioes": sorted({macrorregiao_do_municipio(m, macro_do_municipio) for m in municipios}),
    }

    # sexo, município e macrorregião: filtram as listas antes dos demais
    # cálculos (que já recebem especialidade e status)
    filtro = filtro_paciente(sexo, municipio, macrorregiao, macro_do_municipio)
    linhas = [
        r for r in resumo
        if (not especialidade or r["especialidade"] == especialidade)
        and (not status or r["status"] == status)
        and (filtro is None or filtro(r["sexo"], r["municipio"]))
    ]

    dias = [dt_ini + timedelta(days=i) for i in range((dt_fim - dt_ini).days + 1)]
    idx_dia = {d: i for i, d in enumerate(dias)}
    por_dia = [0] * len(dias)
    for r in linhas:
        por_dia[idx_dia[r["dia"]]] += r["qtd"]

    # horário: início da consulta registrado no atendimento médico
    consultas = _consultas_periodo(dt_ini, dt_fim)
    if filtro is not None:
        consultas = [c for c in consultas if filtro(c[4], c[5])]
    por_hora = [0] * 24
    for esp, st, hora, *_ in consultas:
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
    if filtro is not None:
        visitas = [v for v in visitas if filtro(v[5], v[4])]
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
        "por_macrorregiao": por_macrorregiao(visitas, macro_do_municipio, especialidade, status),
    }


# ---------------------------------------------------------------------------
# Tendência mensal e comparativo (mesmo formato de get_mensal_hospitalar:
# "meses", "meses_iso" e uma série por indicador, um valor por mês)
# ---------------------------------------------------------------------------

# linhas do comparativo, nesta ordem; os atendimentos por status entram logo
# depois de "Atendimentos" (chaves atendimentos_status_<status>)
CAMPOS_MENSAL = [
    ("atendimentos", "Atendimentos", "qtd"),
    ("media_diaria", "Média Diária de Atendimentos", "por dia com atendimento"),
    ("prontuarios", "Prontuários Únicos", "qtd"),
    ("por_prontuario", "Atendimentos por Prontuário", "atend./prontuário"),
    ("concluidos", "Concluídos (concluído + alta)", "qtd"),
    ("taxa_alta", "Taxa de Alta Ambulatorial", "%"),
    ("taxa_absenteismo", "Taxa de Absenteísmo", "%"),
    ("consultas", "Consultas Realizadas (atendimento médico)", "qtd"),
    ("tempo_medio", "Tempo Médio por Consulta", "min"),
    ("tempo_mediano", "Tempo Mediano por Consulta", "min"),
    ("especialidades", "Especialidades com Atendimento", "qtd"),
]


def _janela_meses(mes_ini, mes_fim):
    """Primeiro dia de cada mês de mes_ini a mes_fim ("AAAA-MM", em qualquer
    ordem) e o intervalo de datas que cobre todos eles."""
    a, b = sorted((date.fromisoformat(mes_ini + "-01"), date.fromisoformat(mes_fim + "-01")))
    meses = []
    while a <= b:
        meses.append(a)
        a = (a + timedelta(days=32)).replace(day=1)
    return meses, meses[0], (meses[-1] + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def get_mensal(mes_ini, mes_fim, especialidade=None, sexo=None, municipio=None, macrorregiao=None):
    """Indicadores do ambulatório mês a mês (tendência mensal e comparativo),
    com os filtros de especialidade, sexo, município e macrorregião. Os
    atendimentos são datados pelo cadastro e as consultas (horário/tempo) pelo
    início do atendimento médico, como na página do ambulatório; a média
    diária divide pelos dias em que o ambulatório atendeu (sem filtro)."""
    meses, dt_ini, dt_fim = _janela_meses(mes_ini, mes_fim)
    n = len(meses)
    idx = {(m.year, m.month): i for i, m in enumerate(meses)}
    macro_do_municipio = banco_painel.macrorregiao_dos_municipios()
    filtro = filtro_paciente(sexo, municipio, macrorregiao, macro_do_municipio)

    def passa(esp, sx, mun):
        return (not especialidade or esp == especialidade) and (filtro is None or filtro(sx, mun))

    def serie():
        return [0] * n

    visitas = _visitas_periodo(dt_ini, dt_fim)
    dias_abertos = [set() for _ in meses]
    atend, pacientes, especs = serie(), [set() for _ in meses], [set() for _ in meses]
    por_status, por_sexo_mes, por_macro = {}, {}, {}
    for paciente, esp, st, dia, mun, sx, *_ in visitas:
        i = idx[(dia.year, dia.month)]
        dias_abertos[i].add(dia)
        if not passa(esp, sx, mun):
            continue
        atend[i] += 1
        especs[i].add(esp)
        if paciente is not None:
            pacientes[i].add(paciente)
        por_status.setdefault(st, serie())[i] += 1
        por_sexo_mes.setdefault(sx, serie())[i] += 1
        por_macro.setdefault(macrorregiao_do_municipio(mun, macro_do_municipio), serie())[i] += 1

    consultas, minutos = serie(), [[] for _ in meses]
    for esp, _, _, mins, sx, mun, dia in _consultas_periodo(dt_ini, dt_fim):
        i = idx.get((dia.year, dia.month))
        if i is None or not passa(esp, sx, mun):
            continue
        consultas[i] += 1
        if mins is not None:
            minutos[i].append(mins)

    def do_status(nome):
        return next((v for k, v in por_status.items() if k.upper() == nome), serie())

    def taxa(partes, totais):
        return [round(p / t * 100, 1) if t else None for p, t in zip(partes, totais)]

    def series(contagem, ordem=()):
        nomes = [s for s in ordem if s in contagem] + sorted(
            (s for s in contagem if s not in ordem), key=lambda s: (-sum(contagem[s]), s))
        return [{"nome": s, "valores": contagem[s]} for s in nomes]

    altas = do_status("ALTA AMBULATORIAL")
    faltas = do_status("FALTOU")
    status = series(por_status)
    dados = {
        "meses": _meses_labels(meses),
        "meses_iso": _meses_iso(meses),
        "atendimentos": atend,
        "media_diaria": [round(a / len(d), 1) if d else None for a, d in zip(atend, dias_abertos)],
        "prontuarios": [len(p) for p in pacientes],
        "por_prontuario": [round(a / len(p), 2) if p else None for a, p in zip(atend, pacientes)],
        "concluidos": [c + a for c, a in zip(do_status("CONCLUÍDO"), altas)],
        "altas": altas,
        "faltas": faltas,
        "taxa_alta": taxa(altas, atend),
        "taxa_absenteismo": taxa(faltas, atend),
        "consultas": consultas,
        "tempo_medio": [round(statistics.mean(m), 1) if m else None for m in minutos],
        "tempo_mediano": [round(statistics.median(m), 1) if m else None for m in minutos],
        "especialidades": [len(e) for e in especs],
        "status": status,
        "sexo": series(por_sexo_mes, _ORDEM_SEXO),
        "macrorregiao": series(por_macro),
        # opções dos filtros: tudo o que aparece nos meses (sem filtro aplicado)
        "opcoes": {
            "especialidades": sorted({v[1] for v in visitas}),
            "sexos": sorted({v[5] for v in visitas}),
            "municipios": sorted({v[4] for v in visitas}),
            "macrorregioes": sorted({macrorregiao_do_municipio(v[4], macro_do_municipio) for v in visitas}),
        },
    }
    campos = list(CAMPOS_MENSAL)
    for s in status:
        chave = "atendimentos_status_" + s["nome"]
        dados[chave] = s["valores"]
        campos.insert(campos.index(CAMPOS_MENSAL[0]) + 1 + status.index(s), (chave, s["nome"].capitalize(), "qtd"))
    dados["_campos"] = campos
    return dados
