"""
banco_painel.py

Dados lançados no painel (custos, produção hospitalar e indicadores SIMAQ),
guardados no PostgreSQL configurado em config_banco.py, no schema informado
em "Schema dos dados do painel" na página de configuração do banco (padrão
"painel").

Se o schema não existir e o usuário configurado puder criar schemas, o painel
o cria; senão um administrador do banco roda uma vez sql/painel_schema.sql
(CREATE SCHEMA painel AUTHORIZATION <usuário do painel>). Com o schema
pronto, o painel cria as tabelas e, se estiverem vazias, migra as planilhas
de dados/ (os custos vêm do SQLite painel_dados.db, se já houver lançamentos
nele).

Enquanto o schema não existir, os painéis continuam lendo as planilhas e as
telas de lançamento mostram o que falta fazer.

Cada tabela guarda um valor por mês e item: (mes, chaves...) é único.
"""

import math
import os
import sqlite3
import statistics
import threading
import time
from contextlib import contextmanager
from decimal import Decimal

from psycopg2 import errors as pg_erros
from psycopg2.extras import RealDictCursor, execute_batch

import config_banco

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_DADOS = os.path.join(BASE_DIR, 'dados')
SQLITE_CUSTOS_LEGADO = os.path.join(BASE_DIR, 'painel_dados.db')

# Sem o schema (ou sem banco), tenta de novo no máximo a cada minuto: cada
# tentativa pode levar até o timeout de conexão.
_INTERVALO_NOVA_TENTATIVA = 60


def conectar():
    return config_banco.conectar(connect_timeout=5)


def schema():
    """Schema dos dados do painel, da configuração do banco (validado, pois
    entra direto no SQL)."""
    nome = config_banco.carregar()['schema_painel']
    if not config_banco.schema_valido(nome):
        raise RuntimeError(f'Schema dos dados do painel inválido: {nome!r}.')
    return nome


@contextmanager
def _cursor():
    conn = conectar()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
    finally:
        conn.close()


def ler_numero(texto, formato='decimal'):
    """Número digitado ou vindo da planilha -> float; vazio -> None.

    Aceita o formato brasileiro (1.234.567,89). Sem vírgula, o ponto é
    separador de milhar quando há vários ou quando vem seguido de 3 dígitos
    depois de uma parte inteira diferente de zero (13.831); senão é a casa
    decimal (0.651, 11.37)."""
    if texto is None:
        return None
    if isinstance(texto, (int, float, Decimal)):
        return None if texto != texto else float(texto)  # NaN -> None
    s = str(texto).strip().replace('R$', '').replace(' ', '')
    if not s or s.lower() == 'nan':
        return None
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    elif s.count('.') > 1:
        s = s.replace('.', '')
    elif s.count('.') == 1:
        inteiro, decimais = s.split('.')
        if len(decimais) == 3 and inteiro.lstrip('-+') not in ('', '0'):
            s = inteiro + decimais
    return float(s)


class Coluna:
    """Coluna de valor de uma tabela mensal.

    formato: 'moeda', 'inteiro', 'decimal' ou 'texto'.
    herda:   no lançamento de um mês novo, vem preenchida com o último valor
             do item (ex.: meta do indicador) e não conta para decidir se a
             linha está em branco.
    total:   somada no total do mês.
    do_item: guardada no cadastro do item (vale para todos os meses), não no
             lançamento mensal - só em TabelaNormalizada. padrao = DEFAULT SQL.
    calculada: expressão SQL (aliases c = lançamento, it = item); não é
             gravada, só lida. descricao explica a conta na tela."""

    def __init__(self, nome, rotulo, formato='decimal', herda=False, total=False,
                 do_item=False, padrao=None, calculada=None, descricao=None):
        self.nome, self.rotulo, self.formato, self.herda, self.total = nome, rotulo, formato, herda, total
        self.do_item, self.padrao, self.calculada, self.descricao = do_item, padrao, calculada, descricao

    @property
    def editavel(self):
        """Digitada na tela de lançamento (as do item são mantidas em Cadastros)."""
        return not self.calculada and not self.do_item

    @property
    def do_lancamento(self):
        """Gravada na tabela mensal."""
        return not self.calculada and not self.do_item

    @property
    def tipo_sql(self):
        return 'TEXT' if self.formato == 'texto' else 'NUMERIC'


class ItensNaoCadastrados(ValueError):
    """Lançamento de item que não está nas tabelas de cadastro."""

    def __init__(self, chaves):
        self.chaves = chaves
        amostra = '; '.join(' / '.join(k) for k in chaves[:8])
        mais = f' e mais {len(chaves) - 8}' if len(chaves) > 8 else ''
        super().__init__(f'{len(chaves)} item(ns) não cadastrado(s): {amostra}{mais}. '
                         'Cadastre-os na tela de Cadastros antes de lançar.')


class TabelaMensal:
    def __init__(self, nome, chaves, colunas, planilha, colunas_planilha):
        self.nome = nome
        self.chaves = chaves                    # [(campo, rótulo)] que identificam o item
        self.colunas = colunas                  # [Coluna]
        self.planilha = planilha                # arquivo em dados/ usado na migração
        self.colunas_planilha = colunas_planilha  # campos na ordem das colunas da planilha

    @property
    def tabela(self):
        return f'{schema()}.{self.nome}'

    @property
    def campos_chave(self):
        return [c for c, _ in self.chaves]

    def chave_de(self, linha):
        return tuple(linha[c] for c in self.campos_chave)

    @property
    def colunas_lancamento(self):
        return [c for c in self.colunas if c.do_lancamento]

    def em_branco(self, linha):
        """Sem nenhum valor do mês (herdadas, do item e calculadas não contam)."""
        return all(linha.get(c.nome) is None for c in self.colunas_lancamento if not c.herda)

    def ddl(self):
        campos = ['id SERIAL PRIMARY KEY', 'mes DATE NOT NULL']
        campos += [f'{c} TEXT NOT NULL' for c in self.campos_chave]
        campos += [f'{c.nome} {c.tipo_sql}' for c in self.colunas_lancamento]
        campos += ['atualizado_em TIMESTAMP NOT NULL DEFAULT now()',
                   'atualizado_por TEXT NOT NULL',
                   f"UNIQUE (mes, {', '.join(self.campos_chave)})"]
        return f'CREATE TABLE IF NOT EXISTS {self.tabela} (\n    ' + ',\n    '.join(campos) + '\n)'

    def criar(self, cur):
        cur.execute(self.ddl())

    def _select(self):
        """Consulta das linhas (alias c), no formato usado pelo resto do painel."""
        return f'SELECT * FROM {self.tabela} c'

    @staticmethod
    def _linha(r):
        d = dict(r)
        d['mes'] = d['mes'].strftime('%Y-%m')
        for k, v in d.items():
            if isinstance(v, Decimal):
                d[k] = float(v)
        return d

    # ---------------------------------------------------------------- leitura

    def todos(self):
        with _cursor() as cur:
            cur.execute(f'{self._select()} ORDER BY c.mes')
            return [self._linha(r) for r in cur.fetchall()]

    def dados(self):
        """Linhas para os painéis: do banco; enquanto o schema não existir, da planilha."""
        if pronto():
            return self.todos()
        return self.ler_planilha(os.path.join(PASTA_DADOS, self.planilha))

    def itens(self):
        """Último lançamento de cada item (para montar a grade e herdar valores)."""
        chaves = ', '.join(self.campos_chave)
        with _cursor() as cur:
            cur.execute(f'SELECT DISTINCT ON ({chaves}) * FROM {self.tabela} ORDER BY {chaves}, mes DESC')
            return [self._linha(r) for r in cur.fetchall()]

    def do_mes(self, mes):
        with _cursor() as cur:
            cur.execute(f'{self._select()} WHERE c.mes = %s', [f'{mes}-01'])
            return {self.chave_de(r): self._linha(r) for r in cur.fetchall()}

    def meses(self):
        total = next((c.nome for c in self.colunas if c.total), None)
        soma = f'SUM({total})' if total else 'NULL'
        with _cursor() as cur:
            cur.execute(f"""
                SELECT mes, COUNT(*) AS itens, MAX(atualizado_em) AS atualizado_em, {soma} AS total
                FROM {self.tabela} GROUP BY mes ORDER BY mes DESC
            """)
            return [self._linha(r) for r in cur.fetchall()]

    def contar(self):
        with _cursor() as cur:
            cur.execute(f'SELECT COUNT(*) AS n FROM {self.tabela}')
            return cur.fetchone()['n']

    # ---------------------------------------------------------------- escrita

    def salvar(self, linhas, usuario, criar_cadastros=False):
        """Grava [{mes: 'AAAA-MM', chaves..., colunas...}], substituindo o que já existir."""
        campos = self.campos_chave + [c.nome for c in self.colunas_lancamento]
        sql = f"""
            INSERT INTO {self.tabela} (mes, {', '.join(campos)}, atualizado_em, atualizado_por)
            VALUES (%s, {', '.join(['%s'] * len(campos))}, now(), %s)
            ON CONFLICT (mes, {', '.join(self.campos_chave)}) DO UPDATE SET
                {', '.join(f'{c.nome} = excluded.{c.nome}' for c in self.colunas_lancamento)},
                atualizado_em = excluded.atualizado_em,
                atualizado_por = excluded.atualizado_por
        """
        params = [[f"{l['mes']}-01"] + [l.get(c) for c in campos] + [usuario] for l in linhas]
        with _cursor() as cur:
            execute_batch(cur, sql, params, page_size=500)
        return len(linhas)

    def excluir(self, mes, chaves):
        """Remove os itens [(chave...)] do mês."""
        if not chaves:
            return
        cond = ' AND '.join(f'{c} = %s' for c in self.campos_chave)
        with _cursor() as cur:
            execute_batch(cur, f'DELETE FROM {self.tabela} WHERE mes = %s AND {cond}',
                          [[f'{mes}-01', *k] for k in chaves])

    # ---------------------------------------------------------------- planilha

    def rotulo_campo(self, campo):
        if campo == 'mes':
            return 'Mês'
        return dict(self.chaves).get(campo) or next(c.rotulo for c in self.colunas if c.nome == campo)

    def ler_planilha(self, caminho):
        """Linhas de um xlsx com as colunas na ordem de colunas_planilha
        (a primeira linha é o cabeçalho; os nomes não importam)."""
        import pandas as pd
        df = pd.read_excel(caminho, sheet_name=0)
        n = len(self.colunas_planilha)
        if df.shape[1] < n:
            raise ValueError(f'a planilha precisa de {n} colunas, nesta ordem: '
                             + ', '.join(self.rotulo_campo(c) for c in self.colunas_planilha))
        df = df.iloc[:, :n]
        df.columns = self.colunas_planilha
        # colunas da planilha (as calculadas vêm junto, mas não são gravadas)
        formatos = {c.nome: c.formato for c in self.colunas if c.nome in self.colunas_planilha}
        linhas = {}
        for r in df.to_dict(orient='records'):
            mes = pd.to_datetime(r['mes'], errors='coerce', dayfirst=isinstance(r['mes'], str))
            if pd.isna(mes):
                continue
            linha = {'mes': mes.strftime('%Y-%m')}
            for c in self.campos_chave:
                v = r[c]
                linha[c] = '' if v is None or v != v else str(v).strip()
            if not all(linha[c] for c in self.campos_chave):
                continue
            for c, formato in formatos.items():
                v = r[c]
                if formato == 'texto':
                    linha[c] = None if v is None or v != v or not str(v).strip() else str(v).strip()
                else:
                    linha[c] = ler_numero(v, formato)
            if self.em_branco(linha):
                continue  # como na tela de lançamento: linha sem valor = sem lançamento
            linhas[(linha['mes'], self.chave_de(linha))] = linha  # repetida: vale a última
        return list(linhas.values())

    def importar_planilha(self, caminho, usuario, criar_cadastros=False):
        return self.salvar(self.ler_planilha(caminho), usuario, criar_cadastros)


class TabelaNormalizada(TabelaMensal):
    """Tabela mensal cujos itens ficam em tabelas de cadastro.

    Ex.: custos (cadastros=[('tipo_custo', 'tipo')], item=('item_custo', 'item')):

        tipo_custo (id, nome)
        item_custo (id, tipo_custo_id -> tipo_custo, nome)
        custos     (id, mes, item_custo_id -> item_custo, valor, atualizado_em, atualizado_por)

    Para o resto do painel as linhas continuam com os nomes (tipo, item...),
    como nas outras tabelas mensais; cadastros novos são criados ao salvar.
    Uma tabela ainda no formato antigo (chaves em colunas de texto) é
    convertida em criar()."""

    def __init__(self, nome, chaves, colunas, planilha, colunas_planilha, cadastros, item):
        super().__init__(nome, chaves, colunas, planilha, colunas_planilha)
        self.cadastros = cadastros  # [(tabela, campo)]: cadastros a que o item pertence
        self.item = item            # (tabela, campo): cadastro do item

    @staticmethod
    def _t(nome):
        return f'{schema()}.{nome}'

    @property
    def fk(self):
        return f'{self.item[0]}_id'

    @property
    def _expr(self):
        """campo -> coluna do nome no SELECT (p0, p1... = cadastros; it = item)."""
        expr = {campo: f'p{n}.nome' for n, (_, campo) in enumerate(self.cadastros)}
        expr[self.item[1]] = 'it.nome'
        return expr

    @property
    def _joins(self):
        return ' '.join(f'JOIN {self._t(tab)} p{n} ON p{n}.id = it.{tab}_id'
                        for n, (tab, _) in enumerate(self.cadastros))

    @property
    def _colunas_valor(self):
        return ', '.join(f'{c.nome} {c.tipo_sql}' for c in self.colunas_lancamento)

    @property
    def colunas_item(self):
        return [c for c in self.colunas if c.do_item]

    @staticmethod
    def _def_coluna_item(c):
        return f'{c.nome} {c.tipo_sql}' + (f' NOT NULL DEFAULT {c.padrao}' if c.padrao is not None else '')

    def _expr_coluna(self, c):
        if c.calculada:
            return f'{c.calculada} AS {c.nome}'
        return f'{"it" if c.do_item else "c"}.{c.nome}'

    # ---------------------------------------------------------------- criação

    def criar(self, cur):
        item_tab = self._t(self.item[0])
        for tab, _ in self.cadastros:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._t(tab)} (
                    id   SERIAL PRIMARY KEY,
                    nome TEXT NOT NULL UNIQUE
                )""")
        fks = ''.join(f'{tab}_id INTEGER NOT NULL REFERENCES {self._t(tab)} (id), ' for tab, _ in self.cadastros)
        unicos = ''.join(f'{tab}_id, ' for tab, _ in self.cadastros)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {item_tab} (
                id SERIAL PRIMARY KEY, {fks}
                nome TEXT NOT NULL,
                UNIQUE ({unicos}nome)
            )""")
        for c in self.colunas_item:
            cur.execute(f'ALTER TABLE {item_tab} ADD COLUMN IF NOT EXISTS {self._def_coluna_item(c)}')
        # registro inativo não aparece em mês novo na tela de lançamento (tela Cadastros)
        for tab in [t for t, _ in self.cadastros] + [self.item[0]]:
            cur.execute(f'ALTER TABLE {self._t(tab)} ADD COLUMN IF NOT EXISTS ativo BOOLEAN NOT NULL DEFAULT true')
        if self._formato_antigo(cur):
            self._converter_formato_antigo(cur)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.tabela} (
                id             SERIAL PRIMARY KEY,
                mes            DATE NOT NULL,
                {self.fk}      INTEGER NOT NULL REFERENCES {item_tab} (id),
                {self._colunas_valor},
                atualizado_em  TIMESTAMP NOT NULL DEFAULT now(),
                atualizado_por TEXT NOT NULL,
                UNIQUE (mes, {self.fk})
            )""")

    def _formato_antigo(self, cur):
        cur.execute("""
            SELECT 1 FROM pg_attribute
            WHERE attrelid = to_regclass(%s) AND attname = %s AND NOT attisdropped
        """, [self.tabela, self.item[1]])
        return cur.fetchone() is not None

    def _converter_formato_antigo(self, cur):
        """Chaves em texto -> cadastros + coluna <item>_id. Roda na mesma
        transação de iniciar(): ou converte tudo, ou nada."""
        f, item_tab, (_, item_campo) = self.tabela, self._t(self.item[0]), self.item
        for tab, campo in self.cadastros:
            cur.execute(f'INSERT INTO {self._t(tab)} (nome) SELECT DISTINCT {campo} FROM {f} '
                        f'ON CONFLICT (nome) DO NOTHING')
        joins_pais = ' '.join(f'JOIN {self._t(tab)} p{n} ON p{n}.nome = f.{campo}'
                              for n, (tab, campo) in enumerate(self.cadastros))
        cols_pais = ''.join(f'{tab}_id, ' for tab, _ in self.cadastros)
        ids_pais = ''.join(f'p{n}.id, ' for n in range(len(self.cadastros)))
        cur.execute(f"""
            INSERT INTO {item_tab} ({cols_pais}nome)
            SELECT DISTINCT {ids_pais}f.{item_campo} FROM {f} f {joins_pais}
            ON CONFLICT ({cols_pais}nome) DO NOTHING""")
        cur.execute(f'ALTER TABLE {f} ADD COLUMN {self.fk} INTEGER REFERENCES {item_tab} (id)')
        conds = ' AND '.join([f'p{n}.nome = f.{campo}' for n, (_, campo) in enumerate(self.cadastros)]
                             + [f'it.nome = f.{item_campo}'])
        cur.execute(f"""
            UPDATE {f} f SET {self.fk} = it.id
            FROM {item_tab} it {self._joins}
            WHERE {conds}""")
        cur.execute(f'ALTER TABLE {f} ALTER COLUMN {self.fk} SET NOT NULL')
        # o UNIQUE (mes, chaves...) cai junto com as colunas
        cur.execute(f'ALTER TABLE {f} ' + ', '.join(f'DROP COLUMN {c}' for c in self.campos_chave))
        cur.execute(f'ALTER TABLE {f} ADD CONSTRAINT {self.nome}_mes_{self.fk}_key UNIQUE (mes, {self.fk})')
        print(f'[painel] {f}: {", ".join(self.campos_chave)} convertidos para '
              + ', '.join(self._t(t) for t, _ in self.cadastros + [self.item]))

    # ---------------------------------------------------------------- leitura

    def _select(self):
        nomes = ', '.join(f'{e} AS {c}' for c, e in self._expr.items())
        valores = ', '.join(self._expr_coluna(c) for c in self.colunas)
        return f"""
            SELECT c.id, c.mes, {nomes}, {valores}, c.atualizado_em, c.atualizado_por
            FROM {self.tabela} c
            JOIN {self._t(self.item[0])} it ON it.id = c.{self.fk} {self._joins}"""

    def itens(self):
        """Todos os itens cadastrados (mesmo os que ainda não têm valor lançado).
        'ativo' é falso se o item ou algum cadastro a que pertence estiver inativo."""
        # + atributos do item e, das colunas herdadas do mês (ex.: meta), o último valor lançado
        herdadas = [c.nome for c in self.colunas_lancamento if c.herda]
        ativo = ' AND '.join(['it.ativo'] + [f'p{n}.ativo' for n in range(len(self.cadastros))])
        nomes = ', '.join([f'{e} AS {c}' for c, e in self._expr.items()] + [f'({ativo}) AS ativo']
                          + [f'it.{c.nome}' for c in self.colunas_item] + [f'u.{n}' for n in herdadas])
        ultimo = (f"""LEFT JOIN LATERAL (
                        SELECT {', '.join(herdadas)} FROM {self.tabela} c
                        WHERE c.{self.fk} = it.id ORDER BY c.mes DESC LIMIT 1) u ON true"""
                  if herdadas else '')
        ordem = ', '.join(self._expr[c] for c in self.campos_chave)
        with _cursor() as cur:
            cur.execute(f'SELECT {nomes} FROM {self._t(self.item[0])} it {self._joins} {ultimo} ORDER BY {ordem}')
            return [{k: float(v) if isinstance(v, Decimal) else v for k, v in r.items()} for r in cur.fetchall()]

    # ---------------------------------------------------------------- escrita

    def _ids_itens(self, cur, linhas, criar_cadastros=False):
        """{chave: id do item}. Os itens vêm da tela de Cadastros; só a
        migração inicial (criar_cadastros=True) cadastra o que não existir."""
        if criar_cadastros:
            for tab, campo in self.cadastros:
                execute_batch(cur, f'INSERT INTO {self._t(tab)} (nome) VALUES (%s) ON CONFLICT (nome) DO NOTHING',
                              [[v] for v in sorted({l[campo] for l in linhas})])
            cols_pais = ''.join(f'{tab}_id, ' for tab, _ in self.cadastros)
            sub_pais = ''.join(f'(SELECT id FROM {self._t(tab)} WHERE nome = %s), ' for tab, _ in self.cadastros)
            combinacoes = sorted({tuple(l[c] for _, c in self.cadastros) + (l[self.item[1]],) for l in linhas})
            execute_batch(cur, f"""
                INSERT INTO {self._t(self.item[0])} ({cols_pais}nome) VALUES ({sub_pais}%s)
                ON CONFLICT ({cols_pais}nome) DO NOTHING""", [list(k) for k in combinacoes])
        nomes = ', '.join(f'{e} AS {c}' for c, e in self._expr.items())
        cur.execute(f'SELECT it.id, {nomes} FROM {self._t(self.item[0])} it {self._joins}')
        ids = {self.chave_de(r): r['id'] for r in cur.fetchall()}
        faltando = sorted({self.chave_de(l) for l in linhas} - set(ids))
        if faltando:
            raise ItensNaoCadastrados(faltando)
        return ids

    def salvar(self, linhas, usuario, criar_cadastros=False):
        if not linhas:
            return 0
        nomes = [c.nome for c in self.colunas_lancamento]
        with _cursor() as cur:
            ids = self._ids_itens(cur, linhas, criar_cadastros)
            execute_batch(cur, f"""
                INSERT INTO {self.tabela} (mes, {self.fk}, {', '.join(nomes)}, atualizado_em, atualizado_por)
                VALUES (%s, %s, {', '.join(['%s'] * len(nomes))}, now(), %s)
                ON CONFLICT (mes, {self.fk}) DO UPDATE SET
                    {', '.join(f'{n} = excluded.{n}' for n in nomes)},
                    atualizado_em = excluded.atualizado_em,
                    atualizado_por = excluded.atualizado_por
            """, [[f"{l['mes']}-01", ids[self.chave_de(l)]] + [l.get(n) for n in nomes] + [usuario] for l in linhas],
                page_size=500)
        return len(linhas)

    def excluir(self, mes, chaves):
        if not chaves:
            return
        usando = ', '.join([f'{self._t(self.item[0])} it'] + [f'{self._t(tab)} p{n}' for n, (tab, _) in enumerate(self.cadastros)])
        liga = ' AND '.join([f'c.{self.fk} = it.id'] + [f'it.{tab}_id = p{n}.id' for n, (tab, _) in enumerate(self.cadastros)])
        conds = ' AND '.join(f'{self._expr[c]} = %s' for c in self.campos_chave)
        with _cursor() as cur:
            execute_batch(cur, f'DELETE FROM {self.tabela} c USING {usando} WHERE {liga} AND c.mes = %s AND {conds}',
                          [[f'{mes}-01', *k] for k in chaves])


CUSTOS = TabelaNormalizada(
    'custos',
    [('tipo', 'Tipo de custo'), ('item', 'Item de custo')],
    [Coluna('valor', 'Valor (R$)', 'moeda', total=True)],
    'CUSTO.xlsx', ['mes', 'tipo', 'item', 'valor'],
    cadastros=[('tipo_custo', 'tipo')], item=('item_custo', 'item'),
)
PRODUCAO = TabelaNormalizada(
    'producao_hospitalar',
    [('servico', 'Serviço'), ('tipo', 'Tipo'), ('procedimento', 'Exame / Procedimento')],
    [Coluna('quantidade', 'Quantidade', 'inteiro', total=True)],
    'PRODUÇÃO HOSPITALAR CONSOLIDADA.xlsx', ['mes', 'servico', 'tipo', 'procedimento', 'quantidade'],
    cadastros=[('servico', 'servico'), ('tipo_servico', 'tipo')], item=('procedimento', 'procedimento'),
)
class TabelaSimaq(TabelaNormalizada):
    """Indicadores SIMAQ: tipo_indicador / indicador (com o fator) / simaq.

    O resultado não é gravado: é numerador ÷ denominador × fator do indicador
    (ex.: 1000 na densidade de infecção, por mil pacientes-dia); sem
    denominador (vazio ou 0, como nos indicadores de contagem da planilha),
    o próprio numerador.
    A meta fica no lançamento de cada mês, pois pode variar."""

    def _converter_formato_antigo(self, cur):
        cur.execute(f'SELECT tipo, indicador, numerador, denominador, resultado FROM {self.tabela}')
        antigas = [{k: float(v) if isinstance(v, Decimal) else v for k, v in r.items()} for r in cur.fetchall()]
        super()._converter_formato_antigo(cur)
        cur.execute(f'ALTER TABLE {self.tabela} DROP COLUMN IF EXISTS resultado')
        self._aplicar_fatores(cur, antigas)

    def importar_planilha(self, caminho, usuario, criar_cadastros=False):
        linhas = self.ler_planilha(caminho)
        qtd = self.salvar(linhas, usuario, criar_cadastros)
        if criar_cadastros:  # na migração; depois o fator é mantido na tela de Cadastros
            with _cursor() as cur:
                self._aplicar_fatores(cur, linhas)
        return qtd

    def _aplicar_fatores(self, cur, linhas):
        """Deduz o fator de cada indicador pelos resultados da planilha: se
        resultado ÷ (numerador ÷ denominador) for sempre uma potência de 10
        (ex.: 1000), grava esse fator nos indicadores que ainda estão com 1."""
        razoes = {}
        for l in linhas:
            n, d, r = l.get('numerador'), l.get('denominador'), l.get('resultado')
            if n and d and r:
                razoes.setdefault((l['tipo'], l['indicador']), []).append(r / (n / d))
        for (tipo, indicador), rs in razoes.items():
            m = statistics.median(rs)
            if m <= 0:
                continue
            k = round(math.log10(m))
            if k != 0 and abs(m / 10 ** k - 1) < 0.02:
                cur.execute(f"""
                    UPDATE {self._t('indicador')} it SET fator = %s
                    FROM {self._t('tipo_indicador')} p0
                    WHERE p0.id = it.tipo_indicador_id AND p0.nome = %s AND it.nome = %s AND it.fator = 1
                """, [10 ** k, tipo, indicador])
                print(f'[painel] fator {10 ** k} em "{indicador}"')


SIMAQ = TabelaSimaq(
    'simaq',
    [('tipo', 'Tipo do indicador'), ('indicador', 'Indicador')],
    [Coluna('numerador', 'Numerador'),
     Coluna('denominador', 'Denominador'),
     Coluna('fator', 'Fator', do_item=True, herda=True, padrao=1),
     Coluna('resultado', 'Resultado',
            calculada='ROUND(CASE WHEN COALESCE(c.denominador, 0) = 0 THEN c.numerador '
                      'ELSE c.numerador / c.denominador * it.fator END, 4)',
            descricao='numerador ÷ denominador × fator; sem denominador (vazio ou 0), o próprio numerador'),
     Coluna('meta', 'Meta', herda=True)],
    'INDICADORES SIMAQ.xlsx', ['mes', 'tipo', 'indicador', 'numerador', 'denominador', 'resultado', 'meta'],
    cadastros=[('tipo_indicador', 'tipo')], item=('indicador', 'indicador'),
)
TABELAS = [CUSTOS, PRODUCAO, SIMAQ]


# ---------------------------------------------------------------- cadastros

class Cadastro:
    """Tabela de cadastro mantida na tela Cadastros (tipo_custo, item_custo...).

    pais:   [(tabela, rótulo)] cadastros a que o registro pertence
            (ex.: procedimento -> servico e tipo_servico)
    extras: [Coluna] atributos do registro (ex.: fator do indicador)
    uso:    (tabela, coluna, rótulo) que referencia o registro - um registro
            em uso não pode ser excluído"""

    def __init__(self, tabela, titulo, grupo, pais=(), extras=(), uso=None):
        self.tabela, self.titulo, self.grupo = tabela, titulo, grupo
        self.pais, self.extras, self.uso = list(pais), list(extras), uso

    @property
    def nome_sql(self):
        return f'{schema()}.{self.tabela}'

    def listar(self):
        pais = ''.join(f', p{n}.nome AS {tab}_nome, r.{tab}_id' for n, (tab, _) in enumerate(self.pais))
        joins = ' '.join(f'JOIN {schema()}.{tab} p{n} ON p{n}.id = r.{tab}_id' for n, (tab, _) in enumerate(self.pais))
        extras = ''.join(f', r.{c.nome}' for c in self.extras)
        uso = (f', (SELECT count(*) FROM {schema()}.{self.uso[0]} u WHERE u.{self.uso[1]} = r.id) AS uso'
               if self.uso else ', 0 AS uso')
        ordem = ', '.join([f'p{n}.nome' for n in range(len(self.pais))] + ['r.nome'])
        with _cursor() as cur:
            cur.execute(f'SELECT r.id, r.nome, r.ativo{pais}{extras}{uso} FROM {self.nome_sql} r {joins} ORDER BY {ordem}')
            return [{k: float(v) if isinstance(v, Decimal) else v for k, v in r.items()} for r in cur.fetchall()]

    def definir_ativo(self, registro_id, ativo):
        """Ativa/inativa. Inativo some dos meses novos na tela de lançamento (com
        o que pertence a ele); lançamentos já feitos continuam valendo."""
        with _cursor() as cur:
            cur.execute(f'UPDATE {self.nome_sql} SET ativo = %s WHERE id = %s RETURNING nome', [bool(ativo), registro_id])
            r = cur.fetchone()
        return r['nome'] if r else None

    def salvar(self, registro_id, nome, pais, extras):
        """Cria (registro_id None) ou altera. pais = {tabela: id}; extras = {coluna: valor}
        (valor None mantém o atual / usa o padrão). Retorna mensagem de erro ou None."""
        nome = (nome or '').strip()
        if not nome:
            return 'Informe o nome.'
        for tab, rotulo in self.pais:
            if not pais.get(tab):
                return f'Escolha: {rotulo}.'
        valores = {'nome': nome, **{f'{tab}_id': pais[tab] for tab, _ in self.pais},
                   **{c: v for c, v in extras.items() if v is not None}}
        try:
            with _cursor() as cur:
                if registro_id is None:
                    cur.execute(f"INSERT INTO {self.nome_sql} ({', '.join(valores)}) "
                                f"VALUES ({', '.join(['%s'] * len(valores))})", list(valores.values()))
                else:
                    cur.execute(f"UPDATE {self.nome_sql} SET {', '.join(f'{c} = %s' for c in valores)} WHERE id = %s",
                                list(valores.values()) + [registro_id])
                    if cur.rowcount == 0:
                        return 'Registro não encontrado.'
        except pg_erros.UniqueViolation:
            return f'"{nome}" já está cadastrado' + (' com esses mesmos ' + ' e '.join(r.lower() for _, r in self.pais)
                                                     if self.pais else '') + '.'
        except pg_erros.ForeignKeyViolation:
            return 'Registro relacionado não encontrado; recarregue a página.'
        return None

    def excluir(self, registro_id):
        try:
            with _cursor() as cur:
                cur.execute(f'DELETE FROM {self.nome_sql} WHERE id = %s', [registro_id])
        except pg_erros.ForeignKeyViolation:
            return f'Não pode ser excluído: está em uso ({self.uso[2] if self.uso else "referenciado"}).'
        return None


CADASTROS = [
    Cadastro('tipo_custo', 'Tipos de Custo', 'Custos', uso=('item_custo', 'tipo_custo_id', 'itens')),
    Cadastro('item_custo', 'Itens de Custo', 'Custos', pais=[('tipo_custo', 'Tipo de custo')],
             uso=('custos', 'item_custo_id', 'lançamentos')),
    Cadastro('tipo_servico', 'Tipos de Serviço', 'Produção Hospitalar',
             uso=('procedimento', 'tipo_servico_id', 'procedimentos')),
    Cadastro('servico', 'Serviços', 'Produção Hospitalar', uso=('procedimento', 'servico_id', 'procedimentos')),
    Cadastro('procedimento', 'Procedimentos', 'Produção Hospitalar',
             pais=[('servico', 'Serviço'), ('tipo_servico', 'Tipo')],
             uso=('producao_hospitalar', 'procedimento_id', 'lançamentos')),
    Cadastro('tipo_indicador', 'Tipos de Indicador', 'SIMAQ', uso=('indicador', 'tipo_indicador_id', 'indicadores')),
    Cadastro('indicador', 'Indicadores', 'SIMAQ', pais=[('tipo_indicador', 'Tipo')], extras=SIMAQ.colunas_item,
             uso=('simaq', 'indicador_id', 'lançamentos')),
]
CADASTROS_POR_TABELA = {c.tabela: c for c in CADASTROS}


# ---------------------------------------------------------------- inicialização

_lock = threading.Lock()
_estado = {'pronto': False, 'erro': None, 'tentativa': None}


def sql_criar_schema():
    return f'CREATE SCHEMA IF NOT EXISTS {schema()} AUTHORIZATION {config_banco.carregar()["user"]};'


def _schema_existe(cur):
    cur.execute('SELECT 1 FROM pg_namespace WHERE nspname = %s', [schema()])
    return cur.fetchone() is not None


def _criar_schema(cur):
    """Cria o schema se o usuário configurado tiver permissão. Retorna True se criou."""
    cur.execute("SELECT has_database_privilege(current_user, current_database(), 'CREATE') AS pode")
    if not cur.fetchone()['pode']:
        return False
    cur.execute(f'CREATE SCHEMA IF NOT EXISTS {schema()} AUTHORIZATION CURRENT_USER')
    print(f'[painel] schema {schema()} criado')
    return True


def _linhas_custos_legado():
    """Custos lançados no SQLite usado antes do schema painel (se houver)."""
    if not os.path.exists(SQLITE_CUSTOS_LEGADO):
        return []
    conn = sqlite3.connect(SQLITE_CUSTOS_LEGADO)
    try:
        rows = conn.execute('SELECT mes, tipo, item, valor FROM custos').fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [{'mes': m, 'tipo': t, 'item': i, 'valor': v} for m, t, i, v in rows]


def _migrar(tabela):
    if tabela is CUSTOS:
        legado = _linhas_custos_legado()
        if legado:
            return tabela.salvar(legado, 'migração (painel_dados.db)', criar_cadastros=True)
    caminho = os.path.join(PASTA_DADOS, tabela.planilha)
    if os.path.exists(caminho):
        return tabela.importar_planilha(caminho, f'migração ({tabela.planilha})', criar_cadastros=True)
    return 0


def iniciar(forcar=False):
    """Cria as tabelas no schema painel e migra as planilhas nas tabelas vazias.
    Retorna True se o schema está pronto para uso."""
    with _lock:
        if _estado['pronto']:
            return True
        agora = time.monotonic()
        if not forcar and _estado['tentativa'] is not None and agora - _estado['tentativa'] < _INTERVALO_NOVA_TENTATIVA:
            return False
        _estado['tentativa'] = agora
        try:
            with _cursor() as cur:
                if not _schema_existe(cur) and not _criar_schema(cur):
                    raise RuntimeError(f'O schema "{schema()}" ainda não existe no banco e o usuário configurado '
                                       f'não pode criá-lo. Um administrador do PostgreSQL precisa executar: '
                                       f'{sql_criar_schema()}')
                for t in TABELAS:
                    t.criar(cur)
            for t in TABELAS:
                if t.contar() == 0:
                    qtd = _migrar(t)
                    print(f'[painel] {t.tabela}: {qtd} linhas migradas')
            _estado.update(pronto=True, erro=None)
        except Exception as e:
            _estado['erro'] = str(e)
            print(f'[painel] schema dos dados do painel indisponível: {e}')
        return _estado['pronto']


def reiniciar():
    """Depois de mudar a configuração do banco: esquece o estado e tenta de novo."""
    with _lock:
        _estado.update(pronto=False, erro=None, tentativa=None)
    return iniciar(forcar=True)


def pronto():
    return _estado['pronto'] or iniciar()


def erro():
    return _estado['erro']
