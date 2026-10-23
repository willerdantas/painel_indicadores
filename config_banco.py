"""
config_banco.py

Configuração única de acesso ao PostgreSQL, usada por painel.py,
indicadores_hcasg.py e banco_painel.py. Os dados são informados na página
/admin/banco (só administrador) e ficam criptografados em config_banco.enc.

Campos:
  host, port, dbname, user, password  conexão
  schema         schema do sistema hospitalar (t_internacao, t_exame...),
                 usado como search_path das consultas
  schema_painel  schema onde o painel guarda os dados lançados
                 (custos, produção, SIMAQ) - ver banco_painel.py

Criptografia: Fernet (AES-128-CBC + HMAC-SHA256, biblioteca cryptography).
A chave vem da variável de ambiente PAINEL_CHAVE_CONFIG ou, se ela não
existir, do arquivo .chave_config, gerado no primeiro salvamento. Os dois
arquivos ficam fora do git. Para proteger melhor, guarde a chave na variável
de ambiente do servidor e apague o .chave_config: sem a chave, o
config_banco.enc não pode ser lido.

Um config_banco.json antigo (texto puro) é convertido na primeira leitura e
apagado.

Ordem de prioridade de cada campo: config_banco.enc > variáveis de ambiente
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS (ou DB_PASSWORD), DB_SCHEMA e
DB_SCHEMA_PAINEL > padrões (só porta e schemas; servidor, banco, usuário e
senha não têm padrão).

A configuração vale na hora em que é salva, sem reiniciar o servidor.
"""

import json
import os
import re
import threading
import time

import psycopg2
from cryptography.fernet import Fernet, InvalidToken

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# arquivos locais (chaves, configuração, SQLite): PAINEL_PASTA_LOCAL ou a pasta do projeto
PASTA_LOCAL = os.environ.get('PAINEL_PASTA_LOCAL') or BASE_DIR
ARQUIVO_CONFIG = os.path.join(PASTA_LOCAL, 'config_banco.enc')
ARQUIVO_LEGADO = os.path.join(PASTA_LOCAL, 'config_banco.json')
ARQUIVO_CHAVE = os.path.join(PASTA_LOCAL, '.chave_config')
VARIAVEL_CHAVE = 'PAINEL_CHAVE_CONFIG'

CAMPOS = ('host', 'port', 'dbname', 'user', 'password', 'schema', 'schema_painel')

_PADROES = {
    'host': '',
    'port': '5432',
    'dbname': '',
    'user': '',
    'password': '',
    'schema': 'public',
    'schema_painel': 'painel',
}

_ENV = {
    'host': ('DB_HOST',),
    'port': ('DB_PORT',),
    'dbname': ('DB_NAME',),
    'user': ('DB_USER',),
    'password': ('DB_PASS', 'DB_PASSWORD'),
    'schema': ('DB_SCHEMA',),
    'schema_painel': ('DB_SCHEMA_PAINEL',),
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

_lock = threading.Lock()
# arquivo decifrado em memória, refeito quando o arquivo muda (mtime)
# o arquivo é conferido no máximo a cada _RELEITURA segundos: carregar() roda
# várias vezes por requisição (o schema entra em cada SQL)
_RELEITURA = 2
_cache = {'mtime': None, 'dados': {}, 'lido_em': None}
_estado = {'erro': None}


class ConfiguracaoAusente(RuntimeError):
    pass


# ------------------------------------------------------------------ chave e arquivo

def _fernet(criar=False):
    chave = os.environ.get(VARIAVEL_CHAVE)
    if not chave:
        try:
            with open(ARQUIVO_CHAVE, 'rb') as f:
                chave = f.read().strip()
        except OSError:
            if not criar:
                return None
            chave = Fernet.generate_key()
            with open(ARQUIVO_CHAVE, 'wb') as f:
                f.write(chave)
            try:
                os.chmod(ARQUIVO_CHAVE, 0o600)
            except OSError:
                pass
    return Fernet(chave)


def _gravar(dados):
    token = _fernet(criar=True).encrypt(json.dumps(dados, ensure_ascii=False).encode('utf-8'))
    tmp = ARQUIVO_CONFIG + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(token)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, ARQUIVO_CONFIG)
    _cache['lido_em'] = None  # a próxima leitura já vê o arquivo novo


def _converter_legado():
    """config_banco.json em texto puro -> config_banco.enc."""
    try:
        with open(ARQUIVO_LEGADO, encoding='utf-8') as f:
            dados = json.load(f)
    except (OSError, ValueError):
        return
    if not os.path.exists(ARQUIVO_CONFIG):
        _gravar({k: str(v) for k, v in dados.items() if k in CAMPOS and v not in (None, '')})
    for arq in (ARQUIVO_LEGADO, ARQUIVO_LEGADO + '.tmp'):
        try:
            os.remove(arq)
        except OSError:
            pass


def _ler_arquivo():
    with _lock:
        if _cache['lido_em'] is not None and time.monotonic() - _cache['lido_em'] < _RELEITURA:
            return dict(_cache['dados'])
        _cache['lido_em'] = time.monotonic()
        if os.path.exists(ARQUIVO_LEGADO):
            _converter_legado()
        try:
            mtime = os.path.getmtime(ARQUIVO_CONFIG)
        except OSError:
            _cache.update(mtime=None, dados={})
            _estado['erro'] = None
            return {}
        if mtime == _cache['mtime']:
            return dict(_cache['dados'])
        try:
            fernet = _fernet()  # chave malformada levanta ValueError: tratada abaixo, não derruba as páginas
            if fernet is None:
                raise InvalidToken
            with open(ARQUIVO_CONFIG, 'rb') as f:
                dados = json.loads(fernet.decrypt(f.read()).decode('utf-8'))
        except (InvalidToken, ValueError, OSError):
            _estado['erro'] = ('Não foi possível decifrar config_banco.enc: a chave de criptografia '
                               f'({VARIAVEL_CHAVE} ou .chave_config) não é a que foi usada para salvar. '
                               'Informe a configuração de novo na página de configuração do banco.')
            _cache.update(mtime=None, dados={})
            return {}
        _estado['erro'] = None
        dados = {k: str(v) for k, v in dados.items() if k in CAMPOS and v not in (None, '')}
        _cache.update(mtime=mtime, dados=dados)
        return dict(dados)


def erro_arquivo():
    """Mensagem se o arquivo existe mas não pôde ser decifrado."""
    _ler_arquivo()
    return _estado['erro']


# ------------------------------------------------------------------ configuração

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


def configurado(cfg=None):
    cfg = cfg or carregar()
    return all(cfg.get(c) for c in ('host', 'port', 'dbname', 'user'))


def schema_valido(nome):
    return bool(_SCHEMA_VALIDO.match(nome or ''))


def validar(cfg):
    """Retorna uma lista de mensagens de erro (vazia se estiver tudo certo)."""
    erros = []
    for campo, nome in (('host', 'Servidor'), ('port', 'Porta'), ('dbname', 'Banco'),
                        ('user', 'Usuário'), ('schema', 'Schema do sistema hospitalar'),
                        ('schema_painel', 'Schema dos dados do painel')):
        if not str(cfg.get(campo) or '').strip():
            erros.append(f'{nome} é obrigatório.')
    porta = str(cfg.get('port') or '').strip()
    if porta and (not porta.isdigit() or not 1 <= int(porta) <= 65535):
        erros.append('Porta deve ser um número entre 1 e 65535.')
    for campo, nome in (('schema', 'Schema do sistema hospitalar'), ('schema_painel', 'Schema dos dados do painel')):
        valor = str(cfg.get(campo) or '').strip()
        if valor and not schema_valido(valor):
            erros.append(f'{nome} deve ter só letras, números e "_" (sem começar com número).')
    return erros


def salvar(cfg):
    _gravar({campo: str(cfg.get(campo) or '').strip() for campo in CAMPOS})


def parametros_conexao(cfg=None, **extras):
    """Argumentos para psycopg2.connect a partir da configuração."""
    cfg = cfg or carregar()
    if not configurado(cfg):
        raise ConfiguracaoAusente(erro_arquivo() or 'O banco de dados ainda não foi configurado. '
                                  'Um administrador deve informar os dados em Administração > Configuração do Banco.')
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
    """Tenta conectar com a configuração informada e confere os dois schemas."""
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
            # schema dos dados do painel: existe? o usuário pode criar tabelas nele
            # (ou criar o schema, se ainda não existir)?
            cur.execute("""
                SELECT has_schema_privilege(current_user, n.oid, 'CREATE')
                FROM pg_namespace n WHERE n.nspname = %s
            """, [cfg['schema_painel']])
            r = cur.fetchone()
            painel_existe = r is not None
            if painel_existe:
                painel_pode = bool(r[0])
            else:
                cur.execute("SELECT has_database_privilege(current_user, current_database(), 'CREATE')")
                painel_pode = bool(cur.fetchone()[0])
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
        'schema_painel_existe': painel_existe,
        'schema_painel_pode_criar': painel_pode,
        'sql_schema_painel': f'CREATE SCHEMA IF NOT EXISTS {cfg["schema_painel"]} AUTHORIZATION {usuario};',
    }


def _mensagem_erro(e):
    msg = str(e).strip()
    return msg.splitlines()[0] if msg else 'Falha ao conectar.'
