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
