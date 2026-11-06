"""
paginas_visiveis.py

Quais painéis aparecem no menu inicial. O administrador escolhe na página
/admin/banco; as páginas ocultas ficam em config_paginas.json.

Página oculta: sai do menu e a rota (com as APIs de dados dela) responde
"página desativada" para quem não é administrador. O administrador continua
abrindo pelo endereço, para conferir antes de mostrar de novo.
"""

import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# arquivos locais (chaves, configuração, SQLite): PAINEL_PASTA_LOCAL ou a pasta do projeto
PASTA_LOCAL = os.environ.get('PAINEL_PASTA_LOCAL') or BASE_DIR
ARQUIVO = os.path.join(PASTA_LOCAL, 'config_paginas.json')

# (chave, nome no menu, endpoint, prefixos das rotas)
PAGINAS = [
    ('producao_hospitalar', 'Produção Hospitalar', 'producao_hospitalar', ['/producao_hospitalar']),
    ('simaq', 'Indicadores SIMAQ', 'indicadores', ['/indicadores']),
    ('lean', 'Indicadores LEAN', 'lean_indicadores', ['/lean']),
    ('exames', 'Exames', 'exames_por_clinica', ['/exames_por_clinica', '/graficos_mensais']),
    ('hospitalares', 'Indicadores Hospitalares', 'hcasg_hospitalares',
     ['/hcasg/hospitalares', '/api/hcasg/hospitalares']),
    ('emergencia', 'Indicadores da Emergência', 'hcasg_emergencia',
     ['/hcasg/emergencia', '/api/hcasg/emergencia']),
    ('ambulatorio', 'Ambulatório', 'ambulatorio_pagina', ['/ambulatorio', '/api/ambulatorio']),
    ('custos', 'Custos', 'custos_pagina', ['/custos', '/api/custos']),
    ('financeiro', 'Financeiro', 'financeiro_pagina', ['/financeiro', '/api/financeiro']),
]
CHAVES = [p[0] for p in PAGINAS]

_lock = threading.Lock()


def ocultas():
    """Chaves das páginas ocultas (arquivo ausente ou ilegível: todas visíveis)."""
    try:
        with open(ARQUIVO, encoding='utf-8') as f:
            dados = json.load(f)
    except (OSError, ValueError):
        return set()
    return {c for c in dados.get('ocultas', []) if c in CHAVES}


def salvar(chaves_ocultas):
    escolhidas = set(chaves_ocultas)
    dados = {'ocultas': [c for c in CHAVES if c in escolhidas]}
    with _lock:
        tmp = ARQUIVO + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ARQUIVO)


def listar():
    """Todas as páginas, com a marcação de visível (tela de configuração)."""
    escondidas = ocultas()
    return [{'chave': c, 'nome': n, 'visivel': c not in escondidas} for c, n, _e, _p in PAGINAS]


def visiveis():
    """Cartões do menu inicial: (endpoint, nome, ícone) das páginas visíveis."""
    escondidas = ocultas()
    return [(e, n) for c, n, e, _p in PAGINAS if c not in escondidas]


def pagina_da_rota(caminho):
    for chave, _n, _e, prefixos in PAGINAS:
        if any(caminho == p or caminho.startswith(p + '/') for p in prefixos):
            return chave
    return None


def oculta(caminho):
    chave = pagina_da_rota(caminho)
    return chave is not None and chave in ocultas()
