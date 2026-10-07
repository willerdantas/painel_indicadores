"""
config_banco.py

Configuração única de acesso ao PostgreSQL, usada por painel.py e
indicadores_hcasg.py.

Ordem de prioridade de cada campo:
  1. config_banco.json (salvo pela página /admin/banco)
  2. variáveis de ambiente DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS
     (ou DB_PASSWORD) e DB_SCHEMA
  3. padrões abaixo

A configuração é lida a cada nova conexão, então o que for salvo na página
vale na hora, sem reiniciar o servidor.
"""

import json
import os
import re

import psycopg2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARQUIVO_CONFIG = os.path.join(BASE_DIR, 'config_banco.json')

CAMPOS = ('host', 'port', 'dbname', 'user', 'password', 'schema')

_PADROES = {
    'host': '10.10.28.45',
    'port': '5432',
    'dbname': 'isivitaedb_hm',
    'user': 'postgres',
    'password': 'grandeviagem',
    'schema': 'integra_local',
}

_ENV = {
    'host': ('DB_HOST',),
    'port': ('DB_PORT',),
    'dbname': ('DB_NAME',),
    'user': ('DB_USER',),
    'password': ('DB_PASS', 'DB_PASSWORD'),
    'schema': ('DB_SCHEMA',),
}

# Tabelas que o painel consulta; o teste de conexão confere se existem no schema.
TABELAS_USADAS = (
    't_admissao', 't_clinica', 't_destino', 't_enfermaria', 't_exame',
    't_exame_classificacao', 't_exame_solic', 't_internacao',
    't_internacao_atendimento', 't_internacao_leito', 't_internacao_risco',
    't_leito', 't_leito_interditado', 't_paciente', 't_risco',
    't_tipo_atendimento',
)

_SCHEMA_VALIDO = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _ler_arquivo():
    try:
        with open(ARQUIVO_CONFIG, encoding='utf-8') as f:
            dados = json.load(f)
        return {k: str(v) for k, v in dados.items() if k in CAMPOS and v not in (None, '')}
    except (OSError, ValueError):
        return {}


def carregar():
    """Configuração efetiva (arquivo > ambiente > padrão)."""
    arquivo = _ler_arquivo()
    cfg = {}
    for campo in CAMPOS:
        valor = arquivo.get(campo)
        if valor is None:
            valor = next((os.environ[v] for v in _ENV[campo] if os.environ.get(v)), None)
        cfg[campo] = valor if valor is not None else _PADROES[campo]
    return cfg


def validar(cfg):
    """Retorna uma lista de mensagens de erro (vazia se estiver tudo certo)."""
    erros = []
    for campo, nome in (('host', 'Servidor'), ('port', 'Porta'), ('dbname', 'Banco'),
                        ('user', 'Usuário'), ('schema', 'Schema')):
        if not str(cfg.get(campo) or '').strip():
            erros.append(f'{nome} é obrigatório.')
    porta = str(cfg.get('port') or '').strip()
    if porta and (not porta.isdigit() or not 1 <= int(porta) <= 65535):
        erros.append('Porta deve ser um número entre 1 e 65535.')
    schema = str(cfg.get('schema') or '').strip()
    if schema and not _SCHEMA_VALIDO.match(schema):
        erros.append('Schema deve ter só letras, números e "_" (sem começar com número).')
    return erros


def salvar(cfg):
    dados = {campo: str(cfg.get(campo) or '').strip() for campo in CAMPOS}
    tmp = ARQUIVO_CONFIG + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ARQUIVO_CONFIG)


def parametros_conexao(cfg=None, **extras):
    """Argumentos para psycopg2.connect a partir da configuração."""
    cfg = cfg or carregar()
    params = {
        'host': cfg['host'],
        'port': cfg['port'],
        'dbname': cfg['dbname'],
        'user': cfg['user'],
        'password': cfg['password'],
        # as consultas não fixam schema; vale o search_path da conexão
        'options': f"-c search_path={cfg['schema']}",
    }
    params.update(extras)
    return params


def conectar(**extras):
    return psycopg2.connect(**parametros_conexao(**extras))


def testar(cfg):
    """Tenta conectar com a configuração informada e confere schema e tabelas."""
    try:
        conn = psycopg2.connect(**parametros_conexao(cfg, connect_timeout=5))
    except UnicodeDecodeError:
        # No Windows o servidor responde a falha em pt-BR (latin1) e o
        # psycopg2 não consegue decodificar a mensagem.
        return {'ok': False, 'erro': 'O servidor recusou a conexão. Confira servidor, porta, banco, usuário e senha.'}
    except psycopg2.Error as e:
        return {'ok': False, 'erro': _mensagem_erro(e)}
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT version(), current_user')
            versao, usuario = cur.fetchone()
            cur.execute('SELECT 1 FROM pg_namespace WHERE nspname = %s', [cfg['schema']])
            schema_existe = cur.fetchone() is not None
            cur.execute("""
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s
                  AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
                  AND c.relname = ANY(%s)
            """, [cfg['schema'], list(TABELAS_USADAS)])
            encontradas = {r[0] for r in cur.fetchall()}
    except psycopg2.Error as e:
        return {'ok': False, 'erro': _mensagem_erro(e)}
    finally:
        conn.close()
    return {
        'ok': True,
        'versao': versao.split(' on ')[0],
        'usuario': usuario,
        'schema_existe': schema_existe,
        'tabelas_faltando': [t for t in TABELAS_USADAS if t not in encontradas],
        'total_tabelas': len(TABELAS_USADAS),
    }


def _mensagem_erro(e):
    msg = str(e).strip()
    return msg.splitlines()[0] if msg else 'Falha ao conectar.'
