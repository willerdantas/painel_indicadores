"""
lancamento_custos.py

Lançamento de custos por unidade (centro de custo). O valor de cada despesa
(item de custo) é lançado por unidade em custos_unidade; o total do hospital
por despesa, em custos (usado pela página Custos), é sempre a soma das
unidades: consolidar() o refaz a cada gravação e a cada importação.

Meses lançados antes do lançamento por unidade (só o total, sem nenhuma
unidade: a série migrada de CUSTO.xlsx) são só consulta aqui; lançar uma
unidade neles trocaria o total histórico pela soma de poucas unidades.
"""

from decimal import Decimal

from psycopg2.extras import execute_batch

from banco_painel import _cursor, schema


def _linha(r):
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in r.items()}


def unidades(mes=None):
    """Unidades na ordem dos grupos, com o total e a quantidade de despesas
    lançadas no mês. Inativas só aparecem se tiverem lançamento no mês."""
    s = schema()
    with _cursor() as cur:
        cur.execute(f"""
            SELECT u.id, u.nome, u.medida, g.id AS grupo_id, g.nome AS grupo, (u.ativo AND g.ativo) AS ativo,
                   l.total, COALESCE(l.itens, 0) AS itens
            FROM {s}.unidade u
            JOIN {s}.grupo_unidade g ON g.id = u.grupo_unidade_id
            LEFT JOIN (SELECT unidade_id, SUM(valor) AS total, COUNT(*) AS itens
                       FROM {s}.custos_unidade WHERE mes = %s GROUP BY unidade_id) l ON l.unidade_id = u.id
            ORDER BY g.id, u.id""", [f'{mes}-01' if mes else None])
        return [_linha(r) for r in cur.fetchall() if r['ativo'] or r['itens']]


def itens(mes, unidade_id):
    """Despesas (itens de custo) com o valor lançado na unidade e mês.

    Aparecem as despesas ativas já lançadas por unidade em algum mês e as
    cadastradas que ainda não foram usadas; as da série antiga (só total, nunca
    por unidade) e as inativas ficam de fora, salvo se tiverem valor aqui."""
    s = schema()
    with _cursor() as cur:
        cur.execute(f"""
            SELECT it.id, it.nome AS item, t.nome AS tipo, sc.nome AS subcategoria, (it.ativo AND t.ativo) AS ativo,
                   cu.valor, cu.atualizado_em, cu.atualizado_por,
                   EXISTS (SELECT 1 FROM {s}.custos_unidade x WHERE x.item_custo_id = it.id) AS por_unidade,
                   EXISTS (SELECT 1 FROM {s}.custos x WHERE x.item_custo_id = it.id
                           AND NOT EXISTS (SELECT 1 FROM {s}.custos_unidade y WHERE y.mes = x.mes)) AS no_total
            FROM {s}.item_custo it
            JOIN {s}.tipo_custo t ON t.id = it.tipo_custo_id
            LEFT JOIN {s}.subcategoria_custo sc ON sc.id = it.subcategoria_custo_id
            LEFT JOIN {s}.custos_unidade cu
                   ON cu.item_custo_id = it.id AND cu.mes = %s AND cu.unidade_id = %s
            ORDER BY t.id, sc.nome NULLS LAST, it.id""", [f'{mes}-01', unidade_id])
        return [_linha(r) for r in cur.fetchall()
                if r['valor'] is not None or (r['ativo'] and (r['por_unidade'] or not r['no_total']))]


def _contagem(cur, mes):
    s = schema()
    cur.execute(f"""SELECT (SELECT COUNT(*) FROM {s}.custos WHERE mes = %s) AS despesas,
                           (SELECT COUNT(*) FROM {s}.custos_unidade WHERE mes = %s) AS por_unidade""",
                [f'{mes}-01'] * 2)
    r = dict(cur.fetchone())
    r['so_total'] = r['despesas'] > 0 and r['por_unidade'] == 0
    return r


def situacao(mes):
    """{'despesas': linhas em custos, 'por_unidade': linhas em custos_unidade,
    'so_total': mês antigo, só com o total por despesa (não editável aqui)}."""
    with _cursor() as cur:
        return _contagem(cur, mes)


def despesas_do_mes(mes):
    """Total do mês agrupado por despesa (soma das unidades), com quantas
    unidades lançaram cada uma."""
    s = schema()
    with _cursor() as cur:
        cur.execute(f"""
            SELECT t.nome AS tipo, sc.nome AS subcategoria, it.nome AS item, SUM(cu.valor) AS total, COUNT(*) AS unidades
            FROM {s}.custos_unidade cu
            JOIN {s}.item_custo it ON it.id = cu.item_custo_id
            JOIN {s}.tipo_custo t ON t.id = it.tipo_custo_id
            LEFT JOIN {s}.subcategoria_custo sc ON sc.id = it.subcategoria_custo_id
            WHERE cu.mes = %s
            GROUP BY t.id, t.nome, sc.nome, it.id, it.nome
            ORDER BY t.id, sc.nome NULLS LAST, it.id""", [f'{mes}-01'])
        return [_linha(r) for r in cur.fetchall()]


SEM_SUBCATEGORIA = 'Sem subcategoria'


def agrupar(linhas, campo_valor):
    """Linhas (já ordenadas por categoria e subcategoria) em
    [{'nome', 'total', 'subs': [{'nome', 'total', 'itens'}], 'com_sub'}].
    com_sub é falso quando nenhum item da categoria tem subcategoria (a tela
    então lista os itens direto, sem o subtítulo "Sem subcategoria")."""
    categorias = []
    for l in linhas:
        if not categorias or categorias[-1]['nome'] != l['tipo']:
            categorias.append({'nome': l['tipo'], 'total': 0, 'subs': [], 'com_sub': False})
        cat = categorias[-1]
        sub = l.get('subcategoria') or SEM_SUBCATEGORIA
        if not cat['subs'] or cat['subs'][-1]['nome'] != sub:
            cat['subs'].append({'nome': sub, 'total': 0, 'itens': []})
        cat['com_sub'] = cat['com_sub'] or bool(l.get('subcategoria'))
        valor = l.get(campo_valor) or 0
        cat['subs'][-1]['itens'].append(l)
        cat['subs'][-1]['total'] += valor
        cat['total'] += valor
    return categorias


def meses():
    """Meses com custos: total, despesas e quantas unidades lançaram (0 = mês
    antigo, só com o total)."""
    s = schema()
    with _cursor() as cur:
        cur.execute(f"""
            SELECT to_char(c.mes, 'YYYY-MM') AS mes, COUNT(*) AS despesas, SUM(c.valor) AS total,
                   (SELECT COUNT(DISTINCT cu.unidade_id) FROM {s}.custos_unidade cu WHERE cu.mes = c.mes) AS unidades
            FROM {s}.custos c GROUP BY c.mes ORDER BY c.mes DESC""")
        return [_linha(r) for r in cur.fetchall()]


def ultimo_mes():
    """Último mês lançado por unidade (None se nenhum)."""
    with _cursor() as cur:
        cur.execute(f"SELECT to_char(MAX(mes), 'YYYY-MM') AS mes FROM {schema()}.custos_unidade")
        return cur.fetchone()['mes']


def consolidar(cur, mes):
    """Refaz o total do mês por despesa em custos como a soma das unidades
    (quem alterou por último fica como responsável). Retorna quantas despesas."""
    s = schema()
    cur.execute(f'DELETE FROM {s}.custos WHERE mes = %s', [f'{mes}-01'])
    cur.execute(f"""
        INSERT INTO {s}.custos (mes, item_custo_id, valor, atualizado_em, atualizado_por)
        SELECT mes, item_custo_id, SUM(valor), MAX(atualizado_em),
               (array_agg(atualizado_por ORDER BY atualizado_em DESC))[1]
        FROM {s}.custos_unidade WHERE mes = %s
        GROUP BY mes, item_custo_id""", [f'{mes}-01'])
    return cur.rowcount


class MesSoTotal(ValueError):
    """Mês antigo, só com o total por despesa: não aceita lançamento por unidade."""


def gravar(cur, mes, unidade_id, valores, usuario):
    """Grava os valores {item_custo_id: valor ou None} da unidade no mês (None
    remove) e refaz o total do mês por despesa, no cursor dado (a transação é
    de quem chama). Retorna (gravados, removidos)."""
    s = schema()
    data = f'{mes}-01'
    if _contagem(cur, mes)['so_total']:
        raise MesSoTotal(f'{mes[5:]}/{mes[:4]} foi lançado só com o total por despesa (antes do lançamento por '
                         'unidade); para refazê-lo por unidade, importe a planilha do setor de custos desse mês.')
    novos = [[data, item_id, unidade_id, v, usuario] for item_id, v in valores.items() if v is not None]
    remover = [item_id for item_id, v in valores.items() if v is None]
    execute_batch(cur, f"""
        INSERT INTO {s}.custos_unidade (mes, item_custo_id, unidade_id, valor, atualizado_por)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (mes, item_custo_id, unidade_id) DO UPDATE
            SET valor = excluded.valor, atualizado_em = now(), atualizado_por = excluded.atualizado_por
            WHERE {s}.custos_unidade.valor IS DISTINCT FROM excluded.valor""", novos, page_size=500)
    removidos = 0
    if remover:
        cur.execute(f'DELETE FROM {s}.custos_unidade WHERE mes = %s AND unidade_id = %s AND item_custo_id = ANY(%s)',
                    [data, unidade_id, remover])
        removidos = cur.rowcount
    consolidar(cur, mes)
    return len(novos), removidos


def salvar(mes, unidade_id, valores, usuario):
    with _cursor() as cur:  # tudo ou nada
        return gravar(cur, mes, unidade_id, valores, usuario)
