"""
classificacao_exames.py

Classificação dos exames do painel Exames: cadastro das classificações e a
associação de cada exame do sistema hospitalar (t_exame) a uma delas
(tela /admin/classificacoes).

Tabelas no schema dos dados do painel, criadas com as demais em
banco_painel.iniciar, já montadas: classificações padrão e, uma vez, a
classificação de cada exame que vem com o sistema
(sql/classificacao_exames_padrao.csv) e a da antiga t_exame_classificacao do
sistema hospitalar, quando existir:

  classificacao_exame (id, nome)
  exame_classificacao (isn_exame, classificacao_id, dat_classificacao)

Exame sem associação aparece nos painéis como "SEM CLASSIFICAÇÃO".
"""

import config_banco
import banco_painel
from banco_painel import _cursor, schema


def subconsulta_sql():
    """(isn_exame, classificacao) para o LEFT JOIN das consultas de exames.
    Vazia enquanto o schema do painel não estiver pronto (tudo sem classificação)."""
    if not banco_painel.pronto():
        return '(SELECT NULL::numeric AS isn_exame, NULL::text AS classificacao WHERE false)'
    s = schema()
    return (f'(SELECT ec.isn_exame, c.nome AS classificacao FROM {s}.exame_classificacao ec '
            f'JOIN {s}.classificacao_exame c ON c.id = ec.classificacao_id)')


def nomes():
    """Nomes das classificações (filtros dos painéis)."""
    if not banco_painel.pronto():
        return []
    with _cursor() as cur:
        cur.execute(f'SELECT nome FROM {schema()}.classificacao_exame ORDER BY nome')
        return [r['nome'] for r in cur.fetchall()]


# ---------------------------------------------------------------- cadastro

def listar():
    """Classificações com a quantidade de exames associados."""
    s = schema()
    with _cursor() as cur:
        cur.execute(f"""
            SELECT c.id, c.nome, COUNT(ec.isn_exame) AS exames
            FROM {s}.classificacao_exame c
            LEFT JOIN {s}.exame_classificacao ec ON ec.classificacao_id = c.id
            GROUP BY c.id, c.nome
            ORDER BY c.nome""")
        return [dict(r) for r in cur.fetchall()]


def salvar(classificacao_id, nome):
    """Inclui (classificacao_id None) ou renomeia. Retorna mensagem de erro ou None."""
    nome = ' '.join((nome or '').split())
    if not nome:
        return 'Informe o nome da classificação.'
    s = schema()
    with _cursor() as cur:
        cur.execute(f'SELECT 1 FROM {s}.classificacao_exame WHERE lower(nome) = lower(%s) AND id IS DISTINCT FROM %s',
                    [nome, classificacao_id])
        if cur.fetchone():
            return f'Já existe a classificação "{nome}".'
        if classificacao_id is None:
            cur.execute(f'INSERT INTO {s}.classificacao_exame (nome) VALUES (%s)', [nome])
        else:
            cur.execute(f'UPDATE {s}.classificacao_exame SET nome = %s WHERE id = %s', [nome, classificacao_id])
            if cur.rowcount == 0:
                return 'Classificação não encontrada.'
    return None


def excluir(classificacao_id):
    """Só classificação sem exames. Retorna mensagem de erro ou None."""
    s = schema()
    with _cursor() as cur:
        cur.execute(f'SELECT COUNT(*) AS qtd FROM {s}.exame_classificacao WHERE classificacao_id = %s', [classificacao_id])
        qtd = cur.fetchone()['qtd']
        if qtd:
            return f'A classificação tem {qtd} exame(s) associado(s); mude a classificação deles antes de excluir.'
        cur.execute(f'DELETE FROM {s}.classificacao_exame WHERE id = %s', [classificacao_id])
        if cur.rowcount == 0:
            return 'Classificação não encontrada.'
    return None


# ---------------------------------------------------------------- associação dos exames

def exames():
    """Exames do sistema hospitalar com a classificação atual (id ou None)."""
    conn = config_banco.conectar()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT isn_exame, dsc_exame FROM t_exame ORDER BY dsc_exame, isn_exame')
            lista = cur.fetchall()
    finally:
        conn.close()
    with _cursor() as cur:
        cur.execute(f'SELECT isn_exame, classificacao_id FROM {schema()}.exame_classificacao')
        atual = {int(r['isn_exame']): r['classificacao_id'] for r in cur.fetchall()}
    return [{'isn_exame': int(isn), 'exame': (dsc or '').strip(), 'classificacao_id': atual.get(int(isn))}
            for isn, dsc in lista]


def associar(mudancas):
    """mudancas: {isn_exame: classificacao_id ou None (sem classificação)}.
    Tudo numa transação. Retorna (quantidade alterada, erro ou None)."""
    try:
        mudancas = {int(k): (int(v) if v not in (None, '') else None) for k, v in mudancas.items()}
    except (TypeError, ValueError, AttributeError):
        return 0, 'Dados inválidos.'
    if not mudancas:
        return 0, None
    s = schema()
    with _cursor() as cur:
        cur.execute(f'SELECT id FROM {s}.classificacao_exame')
        validas = {r['id'] for r in cur.fetchall()}
        if any(v is not None and v not in validas for v in mudancas.values()):
            return 0, 'Classificação não encontrada; recarregue a página.'
        remover = [k for k, v in mudancas.items() if v is None]
        if remover:
            cur.execute(f'DELETE FROM {s}.exame_classificacao WHERE isn_exame = ANY(%s)', [remover])
        for isn, cid in mudancas.items():
            if cid is None:
                continue
            cur.execute(f"""
                INSERT INTO {s}.exame_classificacao (isn_exame, classificacao_id) VALUES (%s, %s)
                ON CONFLICT (isn_exame) DO UPDATE
                SET classificacao_id = EXCLUDED.classificacao_id, dat_classificacao = now()""", [isn, cid])
    return len(mudancas), None
