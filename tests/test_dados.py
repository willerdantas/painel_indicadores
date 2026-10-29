"""Números do lançamento, API de custos, SIMAQ e configuração do banco."""
import pytest

import area_restrita
import banco_painel
import config_banco
import custos


# ---------------------------------------------------------------- num_br / ler_numero

@pytest.mark.parametrize('valor, formato, esperado', [
    (0.123456, 'decimal', '0,123456'),
    (1000, 'decimal', '1.000'),
    (2.5, 'inteiro', '2,5'),
    (3, 'inteiro', '3'),
    (1234.5, 'moeda', '1.234,50'),
    (1234.567, 'moeda', '1.234,567'),
    (None, 'decimal', ''),
    ('texto', 'texto', 'texto'),
])
def test_num_br(valor, formato, esperado):
    assert area_restrita.num_br(valor, formato) == esperado


@pytest.mark.parametrize('valor', [0.123456, 2.5, 1234.567, 13831, 0.651, 1000000.25])
def test_num_br_volta_igual_sem_arredondar(valor):
    """Reenviar o formulário sem mexer na linha não muda o valor gravado."""
    assert banco_painel.ler_numero(area_restrita.num_br(valor)) == valor


# ---------------------------------------------------------------- custos

LINHAS_CUSTOS = [
    {'mes': '2024-01', 'tipo': 'Pessoal', 'item': 'Salários', 'valor': 100.0},
    {'mes': '2024-02', 'tipo': 'Pessoal', 'item': 'Salários', 'valor': 110.0},
    {'mes': '2024-05', 'tipo': 'Insumos', 'item': 'Luvas', 'valor': 20.0},
]


@pytest.fixture
def dados_custos(monkeypatch):
    monkeypatch.setattr(banco_painel.CUSTOS, 'dados', lambda: [dict(l) for l in LINHAS_CUSTOS])


def test_custos_periodo(dados_custos):
    d = custos.get_custos('2024-01', '2024-05')
    assert d['meses_iso'] == ['2024-01', '2024-02', '2024-03', '2024-04', '2024-05']
    assert d['kpis']['total'] == 230.0
    assert d['kpis']['meses_com_dado'] == 3


def test_custos_padrao_ultimos_12_meses_completos(dados_custos):
    from datetime import date
    d = custos.get_custos(hoje=date(2024, 6, 15))
    assert (d['mes_ini'], d['mes_fim']) == ('2023-06', '2024-05')
    assert d['meses_disponiveis'][0] == '2023-06' and d['meses_disponiveis'][-1] == '2024-05'
    assert d['kpis']['total'] == 230.0


def test_custos_por_subcategoria(dados_custos, monkeypatch):
    monkeypatch.setattr(custos, '_subcategorias', lambda: {('Pessoal', 'Salários'): 'Servidores'})
    d = custos.get_custos('2024-01', '2024-05')
    assert d['por_subcategoria'] == [{'nome': 'Servidores', 'valor': 210.0},
                                     {'nome': 'Insumos (sem subcategoria)', 'valor': 20.0}]


@pytest.mark.parametrize('mes_ini, mes_fim', [(None, '2024-1'), ('abc', None), ('2024-13', None)])
def test_custos_mes_malformado(dados_custos, mes_ini, mes_fim):
    with pytest.raises(ValueError):
        custos.get_custos(mes_ini, mes_fim)


def test_api_custos_mes_malformado_da_400(cliente, dados_custos):
    r = cliente.get('/api/custos/dados?mes_fim=2024-1')
    assert r.status_code == 400 and not r.get_json()['ok']


def test_sem_banco_e_sem_planilha_fica_vazio(monkeypatch, tmp_path):
    monkeypatch.setattr(banco_painel, 'PASTA_DADOS', str(tmp_path))
    assert banco_painel.CUSTOS.dados() == []
    assert custos.get_custos() == {'vazio': True}


# ---------------------------------------------------------------- SIMAQ

def test_simaq_linha_so_com_resultado_vira_numerador():
    linha = {'numerador': None, 'denominador': None, 'resultado': 42.0, 'meta': None}
    banco_painel.SIMAQ._ajustar_linha_planilha(linha)
    assert linha['numerador'] == 42.0
    assert not banco_painel.SIMAQ.em_branco(linha)


class _CursorFalso:
    def __init__(self):
        self.fatores = {}

    def execute(self, sql, params):
        fator, _tipo, indicador = params
        self.fatores[indicador] = fator


def test_simaq_deduz_fator(monkeypatch, capsys):
    monkeypatch.setattr(banco_painel, 'schema', lambda: 'painel')

    def linha(indicador, n, d, r):
        return {'tipo': 'T', 'indicador': indicador, 'numerador': n, 'denominador': d, 'resultado': r}
    cur = _CursorFalso()
    banco_painel.SIMAQ._aplicar_fatores(cur, [
        linha('por mil', 2, 1000, 2.0), linha('por mil', 3, 1000, 3.0),
        linha('vezes 30', 1, 2, 15.0), linha('vezes 30', 2, 2, 30.0),
        linha('simples', 1, 2, 0.5),
        linha('irregular', 1, 2, 5.0), linha('irregular', 1, 2, 50.0),
    ])
    assert cur.fatores == {'por mil': 1000, 'vezes 30': 30.0}
    assert 'irregular' in capsys.readouterr().out  # vai para o log, para revisar em Cadastros


# ---------------------------------------------------------------- configuração do banco

def test_chave_malformada_nao_derruba(monkeypatch, tmp_path):
    arquivo = tmp_path / 'config_banco.enc'
    arquivo.write_bytes(b'conteudo-cifrado-qualquer')
    monkeypatch.setattr(config_banco, 'ARQUIVO_CONFIG', str(arquivo))
    monkeypatch.setattr(config_banco, 'ARQUIVO_LEGADO', str(tmp_path / 'nao-existe.json'))
    monkeypatch.setenv(config_banco.VARIAVEL_CHAVE, 'isto-nao-e-uma-chave-fernet')
    monkeypatch.setitem(config_banco._cache, 'lido_em', None)
    monkeypatch.setitem(config_banco._cache, 'mtime', None)

    cfg = config_banco.carregar()  # não levanta ValueError
    assert set(cfg) == set(config_banco.CAMPOS)
    assert config_banco.erro_arquivo()


def test_paginas_sem_dados_nao_quebram(cliente, tmp_path, monkeypatch):
    """Servidor novo: sem planilhas em dados/ e sem lançamentos."""
    import painel
    monkeypatch.setattr(banco_painel, 'PASTA_DADOS', str(tmp_path))
    monkeypatch.setattr(painel, 'PASTA_DADOS', str(tmp_path))
    assert cliente.get('/producao_hospitalar').status_code == 200
    assert cliente.get('/indicadores').status_code == 200
    assert cliente.get('/custos').status_code == 200
    r = cliente.get('/financeiro')
    assert r.status_code == 404 and 'Sem dados' in r.get_data(as_text=True)
    assert cliente.get('/faturamento').status_code == 404


def test_subcategoria_cooperativa_pelos_nomes_dos_itens():
    """Só os itens citados entram em Cooperativa; os demais ficam sem subcategoria."""
    nomes = ['CEMERG', 'CCÁRDIO', 'COAPH (Enfermeiros)', 'COAPH (Médicos Generalistas)', 'COMINT',
             'COOPANEST', 'COOPCARDIO', 'COOPCLÍNIC', 'HEMOCOOP', 'Cooperativas', 'Salário / SESA']
    dentro = [n for n in nomes if banco_painel._chave_nome(n).startswith(banco_painel.SUBCATEGORIA_COOPERATIVA)]
    assert dentro == nomes[:6]


def test_cadastro_item_custo_tem_subcategoria_opcional():
    item = banco_painel.CADASTROS_POR_TABELA['item_custo']
    assert item.pais == [('tipo_custo', 'Categoria')]
    assert item.opcionais == [('subcategoria_custo', 'Subcategoria', 'Sem subcategoria', 'tipo_custo')]
    assert banco_painel.CADASTROS_POR_TABELA['tipo_custo'].titulo == 'Categorias de Custo'


def test_lancamento_custos_agrupa_por_categoria_e_subcategoria():
    import lancamento_custos
    linhas = [
        {'tipo': 'Pessoal', 'subcategoria': 'Cooperativa', 'item': 'CEMERG', 'valor': 10.0},
        {'tipo': 'Pessoal', 'subcategoria': 'Cooperativa', 'item': 'COMINT', 'valor': 5.0},
        {'tipo': 'Pessoal', 'subcategoria': None, 'item': 'RESIDENTES', 'valor': None},
        {'tipo': 'Despesas Gerais', 'subcategoria': None, 'item': 'Hemodiálise', 'valor': 2.0},
    ]
    pessoal, gerais = lancamento_custos.agrupar(linhas, 'valor')
    assert pessoal['com_sub'] and pessoal['total'] == 15
    assert [(s['nome'], s['total'], len(s['itens'])) for s in pessoal['subs']] == [
        ('Cooperativa', 15, 2), ('Sem subcategoria', 0, 1)]
    assert not gerais['com_sub'] and gerais['total'] == 2
