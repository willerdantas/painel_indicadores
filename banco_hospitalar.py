"""
banco_hospitalar.py

Acesso ao banco do sistema hospitalar (schema "schema" de config_banco.py):
conexão, consultas com nova tentativa quando a conexão cai, e quais tabelas e
colunas existem no servidor.

Servidor com só parte das tabelas importadas: fetch_all e fetch_one não
executam consulta que cite (FROM/JOIN) uma tabela de
config_banco.TABELAS_USADAS ausente; devolvem vazio, e os indicadores que
dependem dela aparecem vazios ou zerados. As páginas mostram quais tabelas
faltam (faltando_na_pagina, no cabeçalho de _layout.html). Coluna ausente
numa tabela existente não é conferida aqui: a consulta que a usa monta o SQL
com tem_colunas (ver ambulatorio.py).
"""

import re
import threading
import time

import psycopg2
import psycopg2.errorcodes
import psycopg2.extras

import config_banco

_RETRY_ERRORS = {
    psycopg2.errorcodes.ADMIN_SHUTDOWN,
    psycopg2.errorcodes.CRASH_SHUTDOWN,
    psycopg2.errorcodes.CANNOT_CONNECT_NOW,
    psycopg2.errorcodes.CONNECTION_FAILURE,
    psycopg2.errorcodes.SQLCLIENT_UNABLE_TO_ESTABLISH_SQLCONNECTION,
}
_MAX_RETRIES = 4
_RETRY_DELAY = 2


def conectar():
    """Conexão nova com o search_path no schema do sistema hospitalar (quem
    chama fecha). Para várias consultas seguidas na mesma conexão."""
    return config_banco.conectar()


def _should_retry(exc):
    # Falhas de conexão (servidor reiniciando, recusando conexões, conexão
    # caída no meio da consulta) chegam como OperationalError sem pgcode.
    if isinstance(exc, psycopg2.OperationalError) and not exc.pgcode:
        return True
    return exc.pgcode in _RETRY_ERRORS or "conflict with recovery" in str(exc).lower()


def _executar(sql, params, fetch, tentativas=_MAX_RETRIES):
    last_exc = None
    for attempt in range(tentativas):
        conn = None
        try:
            conn = conectar()
            # `with conn` só faz commit/rollback; o close() fica no finally.
            with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params or [])
                return fetch(cur)
        except psycopg2.Error as e:
            last_exc = e
            if _should_retry(e) and attempt < tentativas - 1:
                time.sleep(_RETRY_DELAY)
                continue
            raise
        finally:
            if conn is not None:
                conn.close()
    raise last_exc


def fetch_one(sql, params=None):
    """Primeira linha (dict) ou None; None também se a consulta cita tabela ausente."""
    if _tabelas_ausentes(sql):
        return None
    return _executar(sql, params, lambda cur: cur.fetchone())


def fetch_all(sql, params=None):
    """Linhas (dicts); [] se a consulta cita tabela ausente."""
    if _tabelas_ausentes(sql):
        return []
    return _executar(sql, params, lambda cur: cur.fetchall())


# ------------------------------------------------------------------ tabelas e colunas existentes

_ESTRUTURA_TTL = 300  # segundos: tabela importada depois aparece sozinha
_ESTRUTURA_TTL_FALHA = 30  # banco fora do ar: tenta de novo logo
_estrutura = {'lida_em': None, 'ttl': 0, 'colunas': None}
_estrutura_lock = threading.Lock()


def estrutura():
    """{tabela: {colunas}} das tabelas de config_banco.TABELAS_USADAS que
    existem no schema do sistema hospitalar (em memória por alguns minutos).
    None se não deu para consultar (banco não configurado ou fora do ar):
    aí nada é dado como ausente e as consultas seguem como sempre."""
    with _estrutura_lock:
        lida_em = _estrutura['lida_em']
        if lida_em is not None and time.monotonic() - lida_em < _estrutura['ttl']:
            return _estrutura['colunas']
        try:
            rows = _executar("""
                SELECT table_name, column_name FROM information_schema.columns
                WHERE table_schema = %s AND table_name = ANY(%s)
            """, [config_banco.carregar()['schema'], list(config_banco.TABELAS_USADAS)],
                lambda cur: cur.fetchall(), tentativas=1)
            colunas = {}
            for r in rows:
                colunas.setdefault(r['table_name'], set()).add(r['column_name'])
            _estrutura.update(colunas=colunas, ttl=_ESTRUTURA_TTL)
        except Exception as e:
            print(f"[banco_hospitalar] não deu para ler as tabelas do schema: {e}")
            _estrutura.update(colunas=None, ttl=_ESTRUTURA_TTL_FALHA)
        _estrutura['lida_em'] = time.monotonic()
        return _estrutura['colunas']


def esquecer_estrutura():
    """Relê as tabelas na próxima consulta (depois de mudar a configuração do banco)."""
    with _estrutura_lock:
        _estrutura['lida_em'] = None


def faltando(tabelas):
    """As tabelas da lista que não existem no servidor (lista vazia se não deu para conferir)."""
    colunas = estrutura()
    return [] if colunas is None else [t for t in tabelas if t not in colunas]


def tem_colunas(tabela, *nomes):
    """A tabela existe e tem todas essas colunas? (True se não deu para conferir)."""
    colunas = estrutura()
    return colunas is None or (tabela in colunas and all(n in colunas[tabela] for n in nomes))


_TABELA_NO_SQL = re.compile(r'\b(?:FROM|JOIN)\s+(t_\w+)', re.IGNORECASE)


def _tabelas_ausentes(sql):
    """Tabelas do sistema hospitalar citadas no SQL que faltam no servidor (só
    as de TABELAS_USADAS: outros nomes, como CTEs, não entram)."""
    citadas = {t.lower() for t in _TABELA_NO_SQL.findall(sql)} & set(config_banco.TABELAS_USADAS)
    if not citadas:
        return []
    ausentes = faltando(sorted(citadas))
    if ausentes:
        print(f"[banco_hospitalar] consulta não executada: faltam as tabelas {', '.join(ausentes)}")
    return ausentes


def faltando_na_pagina(chave_pagina):
    """Tabelas que a página (chave de paginas_visiveis) usa e faltam no servidor."""
    return faltando(config_banco.TABELAS_POR_PAGINA.get(chave_pagina, ()))
