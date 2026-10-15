"""
custos.py

Dados da página pública "Custos". Os valores ficam na tabela painel.custos
(banco_painel.py) e são lançados pela tela "Lançar Dados de Custos".
"""

import statistics

import banco_painel


def _somar(linhas, campo):
    tot = {}
    for r in linhas:
        tot[r[campo]] = tot.get(r[campo], 0) + r['valor']
    return sorted(({'nome': k, 'valor': round(v, 2)} for k, v in tot.items()), key=lambda x: (-x['valor'], x['nome']))


def _meses_entre(mes_ini, mes_fim):
    a, m = map(int, mes_ini.split('-'))
    a2, m2 = map(int, mes_fim.split('-'))
    meses = []
    while (a, m) <= (a2, m2):
        meses.append(f'{a:04d}-{m:02d}')
        a, m = (a + 1, 1) if m == 12 else (a, m + 1)
    return meses


def get_custos(mes_ini=None, mes_fim=None, tipo=None, item=None):
    todos = [r for r in banco_painel.CUSTOS.dados() if r.get('valor') is not None]
    if not todos:
        return {'vazio': True}

    meses_disp = sorted({r['mes'] for r in todos})
    if not mes_fim:
        mes_fim = meses_disp[-1]
    if not mes_ini:
        # padrão: os 12 últimos meses com dados
        mes_ini = meses_disp[max(0, len(meses_disp) - 12)]
    if mes_ini > mes_fim:
        mes_ini, mes_fim = mes_fim, mes_ini

    no_periodo = [r for r in todos if mes_ini <= r['mes'] <= mes_fim]
    opcoes = {
        'tipos': sorted({r['tipo'] for r in no_periodo}),
        'itens': sorted({r['item'] for r in no_periodo if not tipo or r['tipo'] == tipo}),
    }
    linhas = [r for r in no_periodo
              if (not tipo or r['tipo'] == tipo) and (not item or r['item'] == item)]

    meses = _meses_entre(mes_ini, mes_fim)
    tipos = sorted({r['tipo'] for r in linhas})
    por_mes_tipo = {t: [0.0] * len(meses) for t in tipos}
    idx = {m: i for i, m in enumerate(meses)}
    for r in linhas:
        por_mes_tipo[r['tipo']][idx[r['mes']]] += r['valor']
    total_mes = [round(sum(por_mes_tipo[t][i] for t in tipos), 2) for i in range(len(meses))]

    total = round(sum(total_mes), 2)
    # média e mediana só dos meses com lançamento (mês sem dado não é custo zero)
    meses_lancados_filtro = {r['mes'] for r in linhas}
    valores_meses_com_dado = [v for v, m in zip(total_mes, meses) if m in meses_lancados_filtro]
    por_tipo = _somar(linhas, 'tipo')
    maior_mes = max(zip(total_mes, meses)) if any(total_mes) else (0, None)

    return {
        'vazio': False,
        'mes_ini': mes_ini,
        'mes_fim': mes_fim,
        'meses_disponiveis': meses_disp,
        'opcoes': opcoes,
        'kpis': {
            'total': total,
            'media_mensal': round(statistics.mean(valores_meses_com_dado), 2) if valores_meses_com_dado else None,
            'mediana_mensal': round(statistics.median(valores_meses_com_dado), 2) if valores_meses_com_dado else None,
            'maior_mes': f'{maior_mes[1][5:]}/{maior_mes[1][:4]}' if maior_mes[1] else None,
            'maior_mes_valor': round(maior_mes[0], 2),
            'maior_tipo': por_tipo[0]['nome'] if por_tipo else None,
            'maior_tipo_valor': por_tipo[0]['valor'] if por_tipo else None,
            'meses_com_dado': len(valores_meses_com_dado),
        },
        'meses': [f'{m[5:]}/{m[:4]}' for m in meses],
        'meses_iso': meses,
        'por_mes_tipo': [{'tipo': t, 'valores': [round(v, 2) for v in por_mes_tipo[t]]} for t in tipos],
        'total_mes': total_mes,
        'por_tipo': por_tipo,
        'por_item': _somar(linhas, 'item'),
    }
