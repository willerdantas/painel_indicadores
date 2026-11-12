"""Servidor com só parte das tabelas do sistema hospitalar importadas: as
consultas com tabela ausente não rodam, as páginas avisam o que falta e o
Ambulatório monta o SQL com as colunas que existem."""
import config_banco


def _estrutura_sem(monkeypatch, *ausentes, colunas_ausentes=()):
    """Estrutura falsa: todas as tabelas usadas, menos as ausentes, com as
    colunas que o Ambulatório confere."""
    import banco_hospitalar
    colunas = {'isn_internacao', 'isn_marcacao', 'isn_especialidade', 'dsc_especialidade',
               'isn_marcacao_situacao', 'dsc_marcacao_situacao', 'isn_tipo_consulta', 'dsc_tipo_consulta', 'isn_municipio',
               'dsc_municipio', 'isn_uf', 'sgl_uf', 'isn_paciente', 'dsc_sexo', 'dat_nascimento', 'isn_cid10'}
    estrutura = {t: set(colunas) for t in config_banco.TABELAS_USADAS if t not in ausentes}
    for tabela, coluna in colunas_ausentes:
        estrutura[tabela].discard(coluna)
    monkeypatch.setattr(banco_hospitalar, 'estrutura', lambda: estrutura)


def test_consulta_com_tabela_ausente_nao_roda(monkeypatch):
    import banco_hospitalar
    _estrutura_sem(monkeypatch, 't_marcacao')
    executadas = []
    monkeypatch.setattr(banco_hospitalar, '_executar', lambda sql, params, fetch, **_: executadas.append(sql) or [{'n': 1}])

    assert banco_hospitalar.fetch_all("SELECT 1 FROM t_internacao ti JOIN t_marcacao tm ON true") == []
    assert banco_hospitalar.fetch_one("select 1 from T_MARCACAO") is None
    assert executadas == []
    # CTEs e tabelas fora de TABELAS_USADAS não contam
    assert banco_hospitalar.fetch_all("WITH atend AS (SELECT 1 FROM t_internacao) SELECT * FROM atend") == [{'n': 1}]
    assert banco_hospitalar.fetch_all("SELECT 1 FROM t_tabela_qualquer") == [{'n': 1}]
    assert len(executadas) == 2


def test_sem_conferir_o_banco_nada_falta(monkeypatch):
    import banco_hospitalar
    monkeypatch.setattr(banco_hospitalar, 'estrutura', lambda: None)
    assert banco_hospitalar.faltando(['t_marcacao']) == []
    assert banco_hospitalar.tem_colunas('t_marcacao', 'isn_especialidade')


def test_faltando_na_pagina(monkeypatch):
    import banco_hospitalar
    _estrutura_sem(monkeypatch, 't_marcacao', 't_exame_solic')
    assert banco_hospitalar.faltando_na_pagina('ambulatorio') == ['t_marcacao']
    assert banco_hospitalar.faltando_na_pagina('exames') == ['t_exame_solic']
    assert banco_hospitalar.faltando_na_pagina('lean') == []
    assert banco_hospitalar.faltando_na_pagina(None) == []


def test_aviso_de_tabelas_faltando_no_cabecalho(monkeypatch):
    from flask import render_template_string
    import painel
    _estrutura_sem(monkeypatch, 't_marcacao', 't_uf')
    modelo = "{% from '_layout.html' import cabecalho %}{{ cabecalho('Teste') }}"
    with painel.app.test_request_context('/ambulatorio'):
        html = render_template_string(modelo)
    assert 'class="aviso-tabelas"' in html and 't_marcacao, t_uf' in html
    with painel.app.test_request_context('/hcasg/hospitalares'):
        assert 'aviso-tabelas' not in render_template_string(modelo)


def test_ambulatorio_nao_le_dsc_especialidade_da_marcacao(monkeypatch):
    """t_marcacao não tem mais dsc_especialidade: o nome vem só de
    t_especialidade pelo isn_especialidade."""
    import ambulatorio
    _estrutura_sem(monkeypatch, colunas_ausentes=[('t_marcacao', 'dsc_especialidade')])
    cte = ambulatorio._cte_marcacao()
    assert 'dsc_especialidade' not in cte and 'tm.isn_especialidade' in cte
    status = ambulatorio._especialidade_status()
    assert 'm.dsc_especialidade' not in status and 'te.dsc_especialidade' in status
    assert 'JOIN t_especialidade te ON te.isn_especialidade = m.isn_especialidade' in \
        ambulatorio._joins_especialidade_status()


def test_ambulatorio_tipo_de_consulta_vem_de_t_tipo_consulta(monkeypatch):
    from decimal import Decimal
    import ambulatorio
    _estrutura_sem(monkeypatch)
    ambulatorio._cache.pop(('tipos_consulta',), None)
    lidas = []
    monkeypatch.setattr(ambulatorio, 'fetch_all', lambda sql, *a: lidas.append(sql) or [
        {'isn_tipo_consulta': Decimal('34'), 'dsc_tipo_consulta': 'RETORNO '},
        {'isn_tipo_consulta': Decimal('4'), 'dsc_tipo_consulta': 'ENFERMAGEM'},  # desativado: entra
        {'isn_tipo_consulta': Decimal('9'), 'dsc_tipo_consulta': '  '}])
    assert ambulatorio.nomes_tipos_consulta() == {34: 'RETORNO', 4: 'ENFERMAGEM'}
    assert 'FROM t_tipo_consulta' in lidas[0]
    # sem a tabela: vazio, sem consultar
    ambulatorio._cache.pop(('tipos_consulta',), None)
    _estrutura_sem(monkeypatch, 't_tipo_consulta')
    assert ambulatorio.nomes_tipos_consulta() == {} and len(lidas) == 1
    ambulatorio._cache.pop(('tipos_consulta',), None)


def test_ambulatorio_sem_isn_especialidade_fica_sem_especialidade(monkeypatch):
    """Servidor sem t_marcacao.isn_especialidade: não há como achar o cadastro."""
    import ambulatorio
    _estrutura_sem(monkeypatch, colunas_ausentes=[('t_marcacao', 'isn_especialidade')])
    cte = ambulatorio._cte_marcacao()
    assert 'tm.isn_especialidade' not in cte and 'NULL::numeric AS isn_especialidade' in cte
    assert 'te.' not in ambulatorio._especialidade_status()
    assert "COALESCE(NULL, 'Sem especialidade')" in ambulatorio._especialidade_status()
    assert 't_especialidade' not in ambulatorio._joins_especialidade_status()


def test_ambulatorio_com_todas_as_tabelas_usa_o_cadastro(monkeypatch):
    import ambulatorio
    _estrutura_sem(monkeypatch)
    assert 'tm.isn_especialidade' in ambulatorio._cte_marcacao()
    assert 'te.dsc_especialidade' in ambulatorio._especialidade_status()
    joins = ambulatorio._joins_especialidade_status() + ambulatorio._joins_municipio_sexo()
    for tabela in ('t_especialidade', 't_marcacao_situacao', 't_municipio', 't_uf', 't_paciente'):
        assert f'JOIN {tabela} ' in joins


def test_emergencia_por_municipio_usa_o_sql_do_ambulatorio(monkeypatch):
    """A Emergência monta o município com as funções do Ambulatório (import
    dentro da função: só quebra quando a rota roda)."""
    import datetime as dt
    import banco_painel
    import indicadores_hcasg
    _estrutura_sem(monkeypatch, 't_uf')
    consultas = []
    monkeypatch.setattr(indicadores_hcasg, 'fetch_all', lambda sql, params=None: consultas.append(sql) or [])
    monkeypatch.setattr(banco_painel, 'macrorregiao_dos_municipios', lambda: {})
    assert indicadores_hcasg._municipios_mensal(dt.date(2026, 10, 1), dt.date(2026, 10, 31), {}) == ([], [])
    assert indicadores_hcasg._atendimentos_por_municipio('WITH atend AS (SELECT 1)', []) == ([], [])
    assert len(consultas) == 2 and all('JOIN t_municipio' in s and 't_uf' not in s for s in consultas)


def _cid_risco(monkeypatch, rows):
    """Roda _atendimentos_por_cid_risco com o banco falso: (resultado, consultas)."""
    import indicadores_hcasg
    consultas = []
    monkeypatch.setattr(indicadores_hcasg, 'fetch_all', lambda sql, params=None: consultas.append(sql) or rows)
    riscos = [{'isn_risco': 1, 'dsc_risco': 'Vermelho '}, {'isn_risco': 2, 'dsc_risco': 'Verde'}]
    return indicadores_hcasg._atendimentos_por_cid_risco('WITH atend AS (SELECT 1)', [], riscos), consultas


def test_emergencia_por_cid_e_risco(monkeypatch):
    _estrutura_sem(monkeypatch)
    m, consultas = _cid_risco(monkeypatch, [
        {'cid': 'J00 - RESFRIADO', 'isn_risco': 2, 'qtd': 5},
        {'cid': 'J00 - RESFRIADO', 'isn_risco': None, 'qtd': 1},
        {'cid': 'Sem CID', 'isn_risco': 1, 'qtd': 2},
        {'cid': 'I10 - HIPERTENSAO', 'isn_risco': 99, 'qtd': 3},  # risco fora do cadastro
    ])
    assert m['riscos'] == ['Vermelho', 'Verde', 'Sem classificação']
    assert m['linhas'] == [
        {'cid': 'J00 - RESFRIADO', 'valores': [0, 5, 1], 'total': 6},
        {'cid': 'I10 - HIPERTENSAO', 'valores': [0, 0, 3], 'total': 3},
        {'cid': 'Sem CID', 'valores': [2, 0, 0], 'total': 2},
    ]
    assert m['totais'] == [2, 5, 4] and m['total'] == 11
    assert 'JOIN risco_final' in consultas[0] and 't_internacao_atendimento' in consultas[0]


def test_emergencia_cid_sem_tabela_de_risco_fica_sem_classificacao(monkeypatch):
    import banco_hospitalar
    _estrutura_sem(monkeypatch, 't_internacao_risco')
    m, consultas = _cid_risco(monkeypatch, [{'cid': 'J00 - RESFRIADO', 'isn_risco': None, 'qtd': 4}])
    assert 'risco_final' not in consultas[0] and banco_hospitalar._tabelas_ausentes(consultas[0]) == []
    assert m['linhas'][0]['valores'] == [0, 0, 4]


def test_emergencia_cid_sem_a_coluna_nao_consulta(monkeypatch):
    """Servidor com t_internacao_atendimento sem isn_cid10: a tabela fica vazia
    e o restante dos atendimentos carrega."""
    _estrutura_sem(monkeypatch, colunas_ausentes=[('t_internacao_atendimento', 'isn_cid10')])
    m, consultas = _cid_risco(monkeypatch, [])
    assert consultas == [] and m['linhas'] == [] and m['total'] == 0


def test_tendencia_cid_e_risco_na_janela_de_meses(monkeypatch):
    import datetime as dt
    import indicadores_hcasg
    _estrutura_sem(monkeypatch)
    consultas = []

    def fetch_all(sql, params=None):
        if 'FROM t_risco' in sql:
            return [{'isn_risco': 1, 'dsc_risco': 'Vermelho'}]
        consultas.append((sql, params))
        return [{'cid': 'J00 - RESFRIADO', 'isn_risco': 1, 'qtd': 7}]

    monkeypatch.setattr(indicadores_hcasg, 'fetch_all', fetch_all)
    m = indicadores_hcasg.get_cid_risco_mensal('2026-01', '2026-02')
    assert m['linhas'] == [{'cid': 'J00 - RESFRIADO', 'valores': [7, 0], 'total': 7}]
    sql, params = consultas[0]
    assert sql.count('%s') == 2 and 'JOIN risco_final' in sql
    assert params == [dt.date(2026, 1, 1), dt.date(2026, 2, 28)]


def test_hospitalares_por_cid_e_destino():
    import indicadores_hcasg
    m = indicadores_hcasg._montar_cid_destino(
        [{'cid': 'I219 - INFARTO', 'qtd': 4}, {'cid': 'Sem CID', 'qtd': 1}, {'cid': 'I200 - ANGINA', 'qtd': 4}],
        [{'cid': 'I219 - INFARTO', 'destino': 'Óbito Institucional', 'isn_destino': 4, 'qtd': 1},
         {'cid': 'I219 - INFARTO', 'destino': 'Alta', 'isn_destino': 1, 'qtd': 2},
         {'cid': 'I200 - ANGINA', 'destino': 'Alta', 'isn_destino': 1, 'qtd': 5},
         {'cid': 'J00 - RESFRIADO', 'destino': 'Óbito Hospitalar', 'isn_destino': 4, 'qtd': 1}])  # sem internação no período
    assert m['destinos'] == ['Alta', 'Óbito Hospitalar', 'Óbito Institucional']
    assert m['linhas'] == [
        {'cid': 'I200 - ANGINA', 'internacoes': 4, 'saidas': [5, 0, 0], 'total': 5},
        {'cid': 'I219 - INFARTO', 'internacoes': 4, 'saidas': [2, 0, 1], 'total': 3},
        {'cid': 'Sem CID', 'internacoes': 1, 'saidas': [0, 0, 0], 'total': 0},
        {'cid': 'J00 - RESFRIADO', 'internacoes': 0, 'saidas': [0, 1, 0], 'total': 1},
    ]
    assert m['totais'] == {'internacoes': 9, 'saidas': [7, 1, 1], 'total': 9}


def test_hospitalares_cid_consultas_com_os_filtros_e_a_janela(monkeypatch):
    import datetime as dt
    import banco_hospitalar
    import indicadores_hcasg
    _estrutura_sem(monkeypatch)
    consultas = []
    monkeypatch.setattr(indicadores_hcasg, 'fetch_all', lambda sql, params=None: consultas.append((sql, params)) or [])
    indicadores_hcasg.get_cid_destino_mensal('UTI', None, None, '2026-01', '2026-02')
    assert len(consultas) == 2
    for sql, params in consultas:
        assert sql.count('%s') == len(params) and 'dsc_clinica = %s' in sql and 'FROM t_admissao ta' in sql
        assert 'UTI' in params and dt.date(2026, 2, 28) in params
        assert banco_hospitalar._tabelas_ausentes(sql) == []
    saidas = next(s for s, _ in consultas if 'internacoes_alvo' in s)
    assert 'Óbito Institucional' in saidas and 'TRUE' in saidas  # uma clínica: inclui o destino 5


def test_hospitalares_cid_sem_a_coluna_nao_consulta(monkeypatch):
    import indicadores_hcasg
    _estrutura_sem(monkeypatch, colunas_ausentes=[('t_admissao', 'isn_cid10')])
    consultas = []
    monkeypatch.setattr(indicadores_hcasg, 'fetch_all', lambda sql, params=None: consultas.append(sql) or [])
    m = indicadores_hcasg.get_cid_destino_hospitalar('2026-01-01', '2026-01-31', None, None, None)
    assert consultas == [] and m['linhas'] == [] and m['totais']['total'] == 0


def test_ambulatorio_sem_tabelas_de_apoio(monkeypatch):
    """Sem marcação, situação, município, UF e paciente: o SQL não cita essas
    tabelas e os campos ficam "Sem ..." (a consulta dos atendimentos roda)."""
    import ambulatorio
    import banco_hospitalar
    ausentes = ('t_marcacao', 't_especialidade', 't_marcacao_situacao', 't_municipio', 't_uf', 't_paciente')
    _estrutura_sem(monkeypatch, *ausentes)
    sql = (ambulatorio._cte_atendimentos() + ambulatorio._especialidade_status() + ambulatorio._municipio_sexo()
           + ambulatorio._joins_especialidade_status() + ambulatorio._joins_municipio_sexo())
    assert not any(t in sql for t in ausentes)
    assert "'Sem especialidade'" in sql and "'Sem marcação'" in sql and "'Sem município'" in sql
    assert banco_hospitalar._tabelas_ausentes(sql) == []
