"""Servidor com só parte das tabelas do sistema hospitalar importadas: as
consultas com tabela ausente não rodam, as páginas avisam o que falta e o
Ambulatório monta o SQL com as colunas que existem."""
import config_banco


def _estrutura_sem(monkeypatch, *ausentes, colunas_ausentes=()):
    """Estrutura falsa: todas as tabelas usadas, menos as ausentes, com as
    colunas que o Ambulatório confere."""
    import banco_hospitalar
    colunas = {'isn_internacao', 'isn_marcacao', 'isn_especialidade', 'dsc_especialidade',
               'isn_marcacao_situacao', 'dsc_marcacao_situacao', 'isn_tipo_consulta', 'isn_municipio',
               'dsc_municipio', 'isn_uf', 'sgl_uf', 'isn_paciente', 'dsc_sexo', 'dat_nascimento'}
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


def test_ambulatorio_sem_isn_especialidade_usa_dsc_especialidade(monkeypatch):
    """Erro em outro servidor: t_marcacao sem isn_especialidade (o PostgreSQL
    sugeria dsc_especialidade)."""
    import ambulatorio
    _estrutura_sem(monkeypatch, colunas_ausentes=[('t_marcacao', 'isn_especialidade')])
    cte = ambulatorio._cte_marcacao()
    assert 'tm.isn_especialidade' not in cte and 'NULL::numeric AS isn_especialidade' in cte
    assert 'tm.dsc_especialidade' in cte
    assert 'te.' not in ambulatorio._especialidade_status()
    assert 'NULLIF(TRIM(m.dsc_especialidade)' in ambulatorio._especialidade_status()
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
