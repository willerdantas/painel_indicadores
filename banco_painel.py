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

import csv
import math
import os
import re
import sqlite3
import statistics
import threading
import time
import unicodedata
from contextlib import contextmanager
from decimal import Decimal

import psycopg2
from psycopg2 import errors as pg_erros
from psycopg2.extras import RealDictCursor, execute_batch

import config_banco

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_DADOS = os.path.join(BASE_DIR, 'dados')
# custos lançados antes do PostgreSQL (só lido na migração)
SQLITE_CUSTOS_LEGADO = os.path.join(os.environ.get('PAINEL_PASTA_LOCAL') or BASE_DIR, 'painel_dados.db')

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
    def do_lancamento(self):
        """Gravada na tabela mensal e digitada na tela de lançamento (as do
        item são mantidas em Cadastros; as calculadas só são lidas)."""
        return not self.calculada and not self.do_item

    editavel = do_lancamento  # nome usado na tela de lançamento

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

    # Criação, consulta e escrita dependem de como os itens são guardados:
    # implementadas em TabelaNormalizada (itens em tabelas de cadastro).

    def criar(self, cur):
        raise NotImplementedError

    def _select(self):
        """Consulta das linhas (alias c), no formato usado pelo resto do painel."""
        raise NotImplementedError

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
        caminho = os.path.join(PASTA_DADOS, self.planilha)
        return self.ler_planilha(caminho) if os.path.exists(caminho) else []

    def itens(self):
        """Todo item ativo ou já lançado, com o último lançamento (para montar
        a grade e herdar valores) e o campo 'ativo'."""
        raise NotImplementedError

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
        """Grava [{mes: 'AAAA-MM', chaves..., colunas...}], substituindo o que
        já existir; criar_cadastros cria os itens que faltarem (migração).
        Retorna quantas linhas gravou."""
        raise NotImplementedError

    def excluir(self, mes, chaves):
        """Remove os itens [(chave...)] do mês."""
        raise NotImplementedError

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
            # texto: dia primeiro (01/08/2026), menos no formato ISO (2026-08-01),
            # que com dayfirst o pandas lê como 8 de janeiro
            texto = isinstance(r['mes'], str)
            iso = texto and re.match(r'\s*\d{4}-\d{1,2}', r['mes'])
            mes = pd.to_datetime(r['mes'], errors='coerce', dayfirst=texto and not iso)
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
            self._ajustar_linha_planilha(linha)
            if self.em_branco(linha):
                continue  # como na tela de lançamento: linha sem valor = sem lançamento
            linhas[(linha['mes'], self.chave_de(linha))] = linha  # repetida: vale a última
        return list(linhas.values())

    def _ajustar_linha_planilha(self, linha):
        """Ajuste específico da tabela em cada linha lida da planilha."""

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

    def itens_faltando(self, linhas):
        """Chaves dos itens das linhas que ainda não estão nos cadastros."""
        nomes = ', '.join(f'{e} AS {c}' for c, e in self._expr.items())
        with _cursor() as cur:
            cur.execute(f'SELECT {nomes} FROM {self._t(self.item[0])} it {self._joins}')
            existentes = {self.chave_de(r) for r in cur.fetchall()}
        return sorted({self.chave_de(l) for l in linhas} - existentes)

    def importar_cadastrando(self, caminho, usuario):
        """Importação da tela: grava a planilha e cadastra os itens (e as
        categorias deles) que ainda não existirem. Retorna (lançamentos
        gravados, [chaves dos itens cadastrados agora])."""
        linhas = self.ler_planilha(caminho)
        novos = self.itens_faltando(linhas)
        qtd = self.salvar(linhas, usuario, criar_cadastros=bool(novos))
        if novos:
            self._depois_de_cadastrar([l for l in linhas if self.chave_de(l) in set(novos)])
        return qtd, novos

    def _depois_de_cadastrar(self, linhas):
        """Ajuste dos itens recém-cadastrados pela importação (linhas só deles)."""

    def _ids_itens(self, cur, linhas, criar_cadastros=False):
        """{chave: id do item}. Os itens vêm da tela de Cadastros; a migração
        inicial e a importação de planilha (criar_cadastros=True) cadastram o
        que não existir."""
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
    [('tipo', 'Categoria de custo'), ('item', 'Item de custo')],
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

    def _ajustar_linha_planilha(self, linha):
        # só o resultado preenchido (indicador de contagem): sem denominador o
        # resultado é o próprio numerador, então ele vira o numerador
        if linha.get('numerador') is None and linha.get('denominador') is None:
            linha['numerador'] = linha.get('resultado')

    def _converter_formato_antigo(self, cur):
        cur.execute(f'UPDATE {self.tabela} SET numerador = resultado '
                    'WHERE numerador IS NULL AND denominador IS NULL AND resultado IS NOT NULL')
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

    def _depois_de_cadastrar(self, linhas):
        # indicador novo: fator deduzido da planilha (os já cadastrados mantêm
        # o fator da tela de Cadastros)
        with _cursor() as cur:
            self._aplicar_fatores(cur, linhas)

    def _aplicar_fatores(self, cur, linhas):
        """Deduz o fator de cada indicador pelos resultados da planilha: se
        resultado ÷ (numerador ÷ denominador) for constante (até 2%), grava
        esse fator (potência de 10 quando for o caso, ex.: 1000) nos
        indicadores que ainda estão com 1. Indicadores cujo resultado não
        segue a conta vão para o log, para o fator ser revisto em Cadastros."""
        razoes = {}
        for l in linhas:
            n, d, r = l.get('numerador'), l.get('denominador'), l.get('resultado')
            if n and d and r:
                razoes.setdefault((l['tipo'], l['indicador']), []).append(r / (n / d))
        for (tipo, indicador), rs in razoes.items():
            m = statistics.median(rs)
            if m <= 0 or any(abs(r / m - 1) >= 0.02 for r in rs):
                print(f'[painel] AVISO: em "{indicador}" o resultado da planilha não é numerador ÷ denominador '
                      f'× um fator fixo (razões de {min(rs):.4g} a {max(rs):.4g}); confira o fator em Cadastros.')
                continue
            k = round(math.log10(m))
            fator = 10 ** k if abs(m / 10 ** k - 1) < 0.02 else float(f'{m:.4g}')
            if fator != 1:
                cur.execute(f"""
                    UPDATE {self._t('indicador')} it SET fator = %s
                    FROM {self._t('tipo_indicador')} p0
                    WHERE p0.id = it.tipo_indicador_id AND p0.nome = %s AND it.nome = %s AND it.fator = 1
                """, [fator, tipo, indicador])
                print(f'[painel] fator {fator} em "{indicador}"')

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
    opcionais: [(tabela, rótulo, texto do vazio, pai comum)] cadastros
            opcionais (coluna <tabela>_id pode ficar nula); o escolhido tem
            de ser do mesmo pai comum que o registro (ex.: item_custo ->
            subcategoria_custo da mesma categoria tipo_custo)
    extras: [Coluna] atributos do registro (ex.: fator do indicador)
    uso:    (tabela, coluna, rótulo) que referencia o registro - um registro
            em uso não pode ser excluído"""

    def __init__(self, tabela, titulo, grupo, pais=(), extras=(), uso=None, opcionais=()):
        self.tabela, self.titulo, self.grupo = tabela, titulo, grupo
        self.pais, self.extras, self.uso = list(pais), list(extras), uso
        self.opcionais = list(opcionais)

    @property
    def nome_sql(self):
        return f'{schema()}.{self.tabela}'

    def listar(self):
        pais = ''.join(f', p{n}.nome AS {tab}_nome, r.{tab}_id' for n, (tab, _) in enumerate(self.pais))
        joins = ' '.join(f'JOIN {schema()}.{tab} p{n} ON p{n}.id = r.{tab}_id' for n, (tab, _) in enumerate(self.pais))
        pais += ''.join(f', o{n}.nome AS {tab}_nome, r.{tab}_id' for n, (tab, *_) in enumerate(self.opcionais))
        joins += ''.join(f' LEFT JOIN {schema()}.{tab} o{n} ON o{n}.id = r.{tab}_id'
                         for n, (tab, *_) in enumerate(self.opcionais))
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

    def salvar(self, registro_id, nome, pais, extras, opcionais=None):
        """Cria (registro_id None) ou altera. pais = {tabela: id}; extras = {coluna: valor}
        (valor None mantém o atual / usa o padrão); opcionais = {tabela: id ou None}.
        Retorna mensagem de erro ou None."""
        nome = (nome or '').strip()
        if not nome:
            return 'Informe o nome.'
        for tab, rotulo in self.pais:
            if not pais.get(tab):
                return f'Escolha: {rotulo}.'
        opcionais = opcionais or {}
        valores = {'nome': nome, **{f'{tab}_id': pais[tab] for tab, _ in self.pais},
                   **{f'{tab}_id': opcionais.get(tab) for tab, *_ in self.opcionais},
                   **{c: v for c, v in extras.items() if v is not None}}
        try:
            with _cursor() as cur:
                for tab, rotulo, _, pai in self.opcionais:
                    if opcionais.get(tab) is None:
                        continue
                    cur.execute(f'SELECT {pai}_id AS pai FROM {schema()}.{tab} WHERE id = %s', [opcionais[tab]])
                    r = cur.fetchone()
                    if r is None:
                        return 'Registro relacionado não encontrado; recarregue a página.'
                    if r['pai'] != pais.get(pai):
                        return f'{rotulo} de outra {dict(self.pais)[pai].lower()}; escolha uma da mesma ou deixe vazia.'
                if registro_id is None:
                    cur.execute(f"INSERT INTO {self.nome_sql} ({', '.join(valores)}) "
                                f"VALUES ({', '.join(['%s'] * len(valores))})", list(valores.values()))
                else:
                    cur.execute(f"UPDATE {self.nome_sql} SET {', '.join(f'{c} = %s' for c in valores)} WHERE id = %s",
                                list(valores.values()) + [registro_id])
                    if cur.rowcount == 0:
                        return 'Registro não encontrado.'
        except pg_erros.UniqueViolation as e:
            # coluna extra única (ex.: código do tipo de consulta) repetida; o
            # UNIQUE da coluna tem o nome padrão do PostgreSQL: <tabela>_<coluna>_key
            repetida = next((c for c in self.extras
                             if e.diag.constraint_name == f'{self.tabela}_{c.nome}_key'), None)
            if repetida:
                return f'{repetida.rotulo} já está em outro registro.'
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
    # categoria de custo: tabela tipo_custo (nome antigo, mantido no banco)
    Cadastro('tipo_custo', 'Categorias de Custo', 'Custos', uso=('item_custo', 'tipo_custo_id', 'itens')),
    Cadastro('subcategoria_custo', 'Subcategorias de Custo', 'Custos', pais=[('tipo_custo', 'Categoria')],
             uso=('item_custo', 'subcategoria_custo_id', 'itens')),
    Cadastro('item_custo', 'Itens de Custo', 'Custos', pais=[('tipo_custo', 'Categoria')],
             opcionais=[('subcategoria_custo', 'Subcategoria', 'Sem subcategoria', 'tipo_custo')],
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
    # unidades (centros de custo) da planilha do setor de custos - importacao_custos.py
    Cadastro('grupo_unidade', 'Grupos de Unidade', 'Unidades (Centros de Custo)',
             uso=('unidade', 'grupo_unidade_id', 'unidades')),
    Cadastro('item_rateio', 'Itens de Rateio', 'Unidades (Centros de Custo)',
             uso=('rateio_unidade', 'item_rateio_id', 'lançamentos')),
    Cadastro('unidade', 'Unidades', 'Unidades (Centros de Custo)', pais=[('grupo_unidade', 'Grupo')],
             extras=[Coluna('medida', 'Medida da produção', 'texto')],
             uso=('custos_unidade', 'unidade_id', 'lançamentos')),
    # agrupamento dos municípios (gráfico por macrorregião do Ambulatório)
    Cadastro('macrorregiao', 'Macrorregiões de Saúde', 'Regiões de Saúde (Ceará)',
             uso=('municipio_regiao', 'macrorregiao_id', 'municípios')),
    Cadastro('municipio_regiao', 'Municípios', 'Regiões de Saúde (Ceará)', pais=[('macrorregiao', 'Macrorregião')]),
    # nome de cada t_marcacao.isn_tipo_consulta (o sistema hospitalar só guarda o código)
    Cadastro('tipo_consulta', 'Tipos de Consulta', 'Ambulatório',
             extras=[Coluna('isn_tipo_consulta', 'Código no sistema (isn_tipo_consulta)', 'inteiro')]),
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


def _criar_tabelas_unidade(cur):
    """Custos por unidade (centro de custo), rateio e produção da planilha do
    setor de custos, e a área de preparação da importação (importacao_custos.py).
    Depende de item_custo (criada por CUSTOS.criar)."""
    s = schema()
    for sql in (
        f"""CREATE TABLE IF NOT EXISTS {s}.grupo_unidade (
                id    SERIAL PRIMARY KEY,
                nome  TEXT NOT NULL UNIQUE,
                ativo BOOLEAN NOT NULL DEFAULT true)""",
        f"""CREATE TABLE IF NOT EXISTS {s}.unidade (
                id               SERIAL PRIMARY KEY,
                grupo_unidade_id INTEGER NOT NULL REFERENCES {s}.grupo_unidade (id),
                nome             TEXT NOT NULL UNIQUE,
                medida           TEXT,
                ativo            BOOLEAN NOT NULL DEFAULT true)""",
        f"""CREATE TABLE IF NOT EXISTS {s}.item_rateio (
                id    SERIAL PRIMARY KEY,
                nome  TEXT NOT NULL UNIQUE,
                ativo BOOLEAN NOT NULL DEFAULT true)""",
        f"""CREATE TABLE IF NOT EXISTS {s}.custos_unidade (
                id             SERIAL PRIMARY KEY,
                mes            DATE NOT NULL,
                item_custo_id  INTEGER NOT NULL REFERENCES {s}.item_custo (id),
                unidade_id     INTEGER NOT NULL REFERENCES {s}.unidade (id),
                valor          NUMERIC NOT NULL,
                atualizado_em  TIMESTAMP NOT NULL DEFAULT now(),
                atualizado_por TEXT NOT NULL,
                UNIQUE (mes, item_custo_id, unidade_id))""",
        f"""CREATE TABLE IF NOT EXISTS {s}.rateio_unidade (
                id             SERIAL PRIMARY KEY,
                mes            DATE NOT NULL,
                item_rateio_id INTEGER NOT NULL REFERENCES {s}.item_rateio (id),
                unidade_id     INTEGER NOT NULL REFERENCES {s}.unidade (id),
                valor          NUMERIC NOT NULL,
                atualizado_em  TIMESTAMP NOT NULL DEFAULT now(),
                atualizado_por TEXT NOT NULL,
                UNIQUE (mes, item_rateio_id, unidade_id))""",
        f"""CREATE TABLE IF NOT EXISTS {s}.producao_unidade (
                id             SERIAL PRIMARY KEY,
                mes            DATE NOT NULL,
                unidade_id     INTEGER NOT NULL REFERENCES {s}.unidade (id),
                producao       NUMERIC NOT NULL,
                atualizado_em  TIMESTAMP NOT NULL DEFAULT now(),
                atualizado_por TEXT NOT NULL,
                UNIQUE (mes, unidade_id))""",
        # preparação: a planilha fica aqui até a confirmação na pré-visualização
        f"""CREATE TABLE IF NOT EXISTS {s}.importacao_custos_lote (
                id           TEXT PRIMARY KEY,
                arquivo      TEXT NOT NULL,
                mes          DATE NOT NULL,
                mes_planilha TEXT,
                custo_total  NUMERIC,
                avisos       TEXT NOT NULL DEFAULT '[]',
                usuario      TEXT NOT NULL,
                criado_em    TIMESTAMP NOT NULL DEFAULT now())""",
        f"""CREATE TABLE IF NOT EXISTS {s}.importacao_custos_linha (
                id            SERIAL PRIMARY KEY,
                lote_id       TEXT NOT NULL REFERENCES {s}.importacao_custos_lote (id) ON DELETE CASCADE,
                categoria     TEXT NOT NULL CHECK (categoria IN ('custo', 'rateio', 'producao')),
                linha         INTEGER NOT NULL,
                coluna        INTEGER,
                tipo          TEXT,
                item          TEXT,
                grupo_unidade TEXT,
                unidade       TEXT,
                medida        TEXT,
                valor         NUMERIC)""",
        # colunas acrescentadas depois da primeira versão da tabela
        f'ALTER TABLE {s}.importacao_custos_lote ADD COLUMN IF NOT EXISTS custo_total NUMERIC',
    ):
        cur.execute(sql)


# Classificações iniciais dos exames (as que existiam na t_exame_classificacao
# do sistema hospitalar); depois são mantidas em /admin/classificacoes.
CLASSIFICACOES_EXAME_PADRAO = (
    'BRONCOSCOPIA', 'ECOCARDIOGRAFIA', 'ELETROFISIOLOGIA', 'ERGOMETRIA', 'ESPIROMETRIA',
    'HEMODINAMICA', 'IMAGEM', 'LABORATORIO', 'OUTROS_PROCEDIMENTOS',
)


def _criar_tabelas_classificacao(cur):
    """Classificação dos exames (classificacao_exames.py). Na primeira vez
    cria as classificações padrão e copia as associações da antiga
    t_exame_classificacao do sistema hospitalar, se ela existir (registrado em
    migracao: excluir depois não traz de volta). Depende de migracao."""
    s = schema()
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {s}.classificacao_exame (
                       id   SERIAL PRIMARY KEY,
                       nome TEXT NOT NULL UNIQUE)""")
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {s}.exame_classificacao (
                       isn_exame         BIGINT PRIMARY KEY,
                       classificacao_id  INTEGER NOT NULL REFERENCES {s}.classificacao_exame (id),
                       dat_classificacao TIMESTAMP NOT NULL DEFAULT now())""")
    cur.execute(f"SELECT 1 FROM {s}.migracao WHERE tabela = 'exame_classificacao'")
    if cur.fetchone():
        return
    execute_batch(cur, f'INSERT INTO {s}.classificacao_exame (nome) VALUES (%s) ON CONFLICT (nome) DO NOTHING',
                  [(n,) for n in CLASSIFICACOES_EXAME_PADRAO])
    linhas = 0
    # tabela antiga, no schema do sistema hospitalar (search_path da conexão)
    cur.execute("SELECT to_regclass('t_exame_classificacao') IS NOT NULL AS existe")
    if cur.fetchone()['existe']:
        cur.execute('SAVEPOINT migra_classificacao')
        try:
            cur.execute(f"""INSERT INTO {s}.classificacao_exame (nome)
                           SELECT DISTINCT classificacao FROM t_exame_classificacao
                           WHERE classificacao IS NOT NULL
                           ON CONFLICT (nome) DO NOTHING""")
            cur.execute(f"""INSERT INTO {s}.exame_classificacao (isn_exame, classificacao_id, dat_classificacao)
                           SELECT t.isn_exame, c.id, COALESCE(t.dat_classificacao, now())
                           FROM t_exame_classificacao t
                           JOIN {s}.classificacao_exame c ON c.nome = t.classificacao
                           ON CONFLICT (isn_exame) DO NOTHING""")
            linhas = cur.rowcount
        except psycopg2.Error as e:  # tabela antiga diferente: segue só com as padrão
            cur.execute('ROLLBACK TO SAVEPOINT migra_classificacao')
            print(f'[painel] AVISO: classificações de exames não copiadas de t_exame_classificacao: {e}')
    cur.execute(f"INSERT INTO {s}.migracao (tabela, linhas) VALUES ('exame_classificacao', %s)", [linhas])
    if linhas:
        print(f'[painel] exame_classificacao: {linhas} exames classificados (de t_exame_classificacao)')


# Classificação de cada exame que vem com o sistema (versionada no projeto,
# junto do SQL de apoio; gerada da classificação existente em 29/10/2026):
# isn_exame;exame;classificacao
ARQUIVO_CLASSIFICACOES_EXAME = os.path.join(BASE_DIR, 'sql', 'classificacao_exames_padrao.csv')


def _ler_classificacoes_padrao():
    """[(isn_exame, exame, classificacao)] do arquivo do sistema; vazio sem ele."""
    if not os.path.exists(ARQUIVO_CLASSIFICACOES_EXAME):
        return []
    with open(ARQUIVO_CLASSIFICACOES_EXAME, encoding='utf-8', newline='') as f:
        return [(int(r['isn_exame']), r['exame'].strip(), r['classificacao'].strip())
                for r in csv.DictReader(f, delimiter=';')]


def _carregar_classificacoes_padrao(cur):
    """Uma vez (registrado em migracao), monta a classificação dos exames com
    o arquivo do sistema, para um servidor novo já começar com os exames
    classificados (sem depender da antiga t_exame_classificacao). Só entra em
    exame ainda sem classificação: o que foi feito em /admin/classificacoes
    não muda. O exame é achado em t_exame pelo nome, preferindo o mesmo código
    (outra base pode ter outros códigos); sem acesso a t_exame, pelo código.
    Depende de _criar_tabelas_classificacao."""
    s = schema()
    cur.execute(f"SELECT 1 FROM {s}.migracao WHERE tabela = 'exame_classificacao_padrao'")
    if cur.fetchone():
        return
    padrao = _ler_classificacoes_padrao()
    if not padrao:
        # sem o arquivo não registra a carga: tenta de novo no próximo início
        print(f'[painel] AVISO: {ARQUIVO_CLASSIFICACOES_EXAME} não encontrado; '
              'exames sem a classificação padrão do sistema')
        return
    execute_batch(cur, f'INSERT INTO {s}.classificacao_exame (nome) VALUES (%s) ON CONFLICT (nome) DO NOTHING',
                  [(c,) for c in sorted({c for _, _, c in padrao})])
    cur.execute('CREATE TEMP TABLE classificacao_padrao (isn_exame BIGINT, exame TEXT, classificacao TEXT) '
                'ON COMMIT DROP')
    execute_batch(cur, 'INSERT INTO classificacao_padrao VALUES (%s, %s, %s)', padrao)
    cur.execute('SAVEPOINT classificacao_padrao')
    try:
        cur.execute(f"""INSERT INTO {s}.exame_classificacao (isn_exame, classificacao_id)
                       SELECT DISTINCT ON (te.isn_exame) te.isn_exame, c.id
                       FROM classificacao_padrao p
                       JOIN t_exame te ON upper(trim(te.dsc_exame)) = upper(p.exame)
                       JOIN {s}.classificacao_exame c ON c.nome = p.classificacao
                       ORDER BY te.isn_exame, (te.isn_exame = p.isn_exame) DESC
                       ON CONFLICT (isn_exame) DO NOTHING""")
    except psycopg2.Error as e:  # t_exame inacessível: pelo código
        cur.execute('ROLLBACK TO SAVEPOINT classificacao_padrao')
        print(f'[painel] AVISO: t_exame indisponível, classificação padrão pelo código do exame: {e}')
        cur.execute(f"""INSERT INTO {s}.exame_classificacao (isn_exame, classificacao_id)
                       SELECT p.isn_exame, c.id FROM classificacao_padrao p
                       JOIN {s}.classificacao_exame c ON c.nome = p.classificacao
                       ON CONFLICT (isn_exame) DO NOTHING""")
    linhas = cur.rowcount
    cur.execute(f"INSERT INTO {s}.migracao (tabela, linhas) VALUES ('exame_classificacao_padrao', %s)", [linhas])
    if linhas:
        print(f'[painel] exame_classificacao: {linhas} exames classificados (classificação padrão do sistema)')


# Subcategoria inicial: Cooperativa (categoria Pessoal) com os itens que
# começam por estes nomes; os demais itens ficam sem subcategoria até
# serem associados na tela Cadastros.
SUBCATEGORIA_COOPERATIVA = ('CEMERG', 'CCARDIO', 'COAPH', 'COMINT', 'COOPANEST')


def _chave_nome(nome):
    sem_acento = unicodedata.normalize('NFKD', nome or '').encode('ascii', 'ignore').decode()
    return ' '.join(sem_acento.upper().split())


def _criar_subcategorias_custo(cur):
    """subcategoria_custo (dentro de uma categoria tipo_custo) e a coluna
    opcional item_custo.subcategoria_custo_id. Na primeira vez cria a
    Cooperativa em Pessoal (registrado em migracao). Depende de migracao."""
    s = schema()
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {s}.subcategoria_custo (
                       id            SERIAL PRIMARY KEY,
                       tipo_custo_id INTEGER NOT NULL REFERENCES {s}.tipo_custo (id),
                       nome          TEXT NOT NULL,
                       ativo         BOOLEAN NOT NULL DEFAULT true,
                       UNIQUE (tipo_custo_id, nome))""")
    cur.execute(f'ALTER TABLE {s}.item_custo ADD COLUMN IF NOT EXISTS '
                f'subcategoria_custo_id INTEGER REFERENCES {s}.subcategoria_custo (id)')
    cur.execute(f"SELECT 1 FROM {s}.migracao WHERE tabela = 'subcategoria_custo'")
    if cur.fetchone():
        return
    linhas = 0
    cur.execute(f'SELECT id, nome FROM {s}.tipo_custo')
    pessoal = next((r['id'] for r in cur.fetchall() if _chave_nome(r['nome']) == 'PESSOAL'), None)
    if pessoal is not None:
        cur.execute(f"""INSERT INTO {s}.subcategoria_custo (tipo_custo_id, nome) VALUES (%s, 'Cooperativa')
                        ON CONFLICT (tipo_custo_id, nome) DO UPDATE SET nome = EXCLUDED.nome RETURNING id""",
                    [pessoal])
        cooperativa = cur.fetchone()['id']
        cur.execute(f'SELECT id, nome FROM {s}.item_custo WHERE tipo_custo_id = %s AND subcategoria_custo_id IS NULL',
                    [pessoal])
        ids = [r['id'] for r in cur.fetchall() if _chave_nome(r['nome']).startswith(SUBCATEGORIA_COOPERATIVA)]
        if ids:
            cur.execute(f'UPDATE {s}.item_custo SET subcategoria_custo_id = %s WHERE id = ANY(%s)', [cooperativa, ids])
            linhas = len(ids)
            print(f'[painel] subcategoria Cooperativa (Pessoal): {linhas} itens')
    cur.execute(f"INSERT INTO {s}.migracao (tabela, linhas) VALUES ('subcategoria_custo', %s)", [linhas])


# Macrorregiões de saúde do Ceará e seus municípios (grafia do cadastro
# t_municipio do sistema hospitalar: sem acento, maiúsculas). Fonte: Plano
# Estadual de Saúde 2020-2023 (SESA-CE), quadros de cobertura por município de
# cada região de saúde. Carga inicial de municipio_regiao; depois a tela Cadastros.
MACRORREGIOES_CE = {
    'Fortaleza': (  # 44 municípios
        'ACARAPE', 'AMONTADA', 'APUIARES', 'AQUIRAZ', 'ARACOIABA', 'ARATUBA', 'BARREIRA', 'BATURITE',
        'BEBERIBE', 'CAPISTRANO', 'CASCAVEL', 'CAUCAIA', 'CHOROZINHO', 'EUSEBIO', 'FORTALEZA',
        'GENERAL SAMPAIO', 'GUAIUBA', 'GUARAMIRANGA', 'HORIZONTE', 'ITAITINGA', 'ITAPAJE', 'ITAPIPOCA',
        'ITAPIUNA', 'MARACANAU', 'MARANGUAPE', 'MIRAIMA', 'MULUNGU', 'OCARA', 'PACAJUS', 'PACATUBA', 'PACOTI',
        'PALMACIA', 'PARACURU', 'PARAIPABA', 'PENTECOSTE', 'PINDORETAMA', 'REDENCAO',
        'SAO GONCALO DO AMARANTE', 'SAO LUIS DO CURU', 'TEJUCUOCA', 'TRAIRI', 'TURURU', 'UMIRIM',
        'URUBURETAMA',
    ),
    'Norte': (  # 55 municípios
        'ACARAU', 'ALCANTARAS', 'ARARENDA', 'BARROQUINHA', 'BELA CRUZ', 'CAMOCIM', 'CARIRE', 'CARNAUBAL',
        'CATUNDA', 'CHAVAL', 'COREAU', 'CRATEUS', 'CROATA', 'CRUZ', 'FORQUILHA', 'FREICHEIRINHA', 'GRACA',
        'GRANJA', 'GROAIRAS', 'GUARACIABA DO NORTE', 'HIDROLANDIA', 'IBIAPINA', 'INDEPENDENCIA', 'IPAPORANGA',
        'IPU', 'IPUEIRAS', 'IRAUCUBA', 'ITAREMA', 'JERICOACOARA', 'MARCO', 'MARTINOPOLE', 'MASSAPE', 'MERUOCA',
        'MONSENHOR TABOSA', 'MORAUJO', 'MORRINHOS', 'MUCAMBU', 'NOVA RUSSAS', 'NOVO ORIENTE', 'PACUJA',
        'PIRES FERREIRA', 'PORANGA', 'QUITERIANOPOLIS', 'RERIUTABA', 'SANTA QUITERIA', 'SANTANA DO ACARAU',
        'SAO BENEDITO', 'SENADOR SA', 'SOBRAL', 'TAMBORIL', 'TIANGUA', 'UBAJARA', 'URUOCA', 'VARJOTA',
        'VICOSA DO CEARA',
    ),
    'Cariri': (  # 45 municípios
        'ABAIARA', 'ACOPIARA', 'ALTANEIRA', 'ANTONINA DO NORTE', 'ARARIPE', 'ASSARE', 'AURORA', 'BAIXIO',
        'BARBALHA', 'BARRO', 'BREJO SANTO', 'CAMPOS SALES', 'CARIRIACU', 'CARIUS', 'CATARINA', 'CEDRO',
        'CRATO', 'DEPUTADO IRAPUAN PINHEIRO', 'FARIAS BRITO', 'GRANJEIRO', 'ICO', 'IGUATU', 'IPAUMIRIM',
        'JARDIM', 'JATI', 'JUAZEIRO DO NORTE', 'JUCAS', 'LAVRAS DA MANGABEIRA', 'MAURITI', 'MILAGRES',
        'MISSAO VELHA', 'MOMBACA', 'NOVA OLINDA', 'OROS', 'PENAFORTE', 'PIQUET CARNEIRO', 'PORTEIRAS',
        'POTENGI', 'QUIXELO', 'SABOEIRO', 'SALITRE', 'SANTANA DO CARIRI', 'TARRAFAS', 'UMARI', 'VARZEA ALEGRE',
    ),
    'Sertão Central': (  # 20 municípios
        'AIUABA', 'ARNEIROZ', 'BANABUIU', 'BOA VIAGEM', 'CANINDE', 'CARIDADE', 'CHORO', 'IBARETAMA',
        'IBICUITINGA', 'ITATIRA', 'MADALENA', 'MILHA', 'PARAMBU', 'PARAMOTI', 'PEDRA BRANCA', 'QUIXADA',
        'QUIXERAMOBIM', 'SENADOR POMPEU', 'SOLONOPOLE', 'TAUA',
    ),
    'Litoral Leste/Jaguaribe': (  # 20 municípios
        'ALTO SANTO', 'ARACATI', 'ERERE', 'FORTIM', 'ICAPUI', 'IRACEMA', 'ITAICABA', 'JAGUARETAMA',
        'JAGUARIBARA', 'JAGUARIBE', 'JAGUARUANA', 'LIMOEIRO DO NORTE', 'MORADA NOVA', 'PALHANO', 'PEREIRO',
        'POTIRETAMA', 'QUIXERE', 'RUSSAS', 'SAO JOAO DO JAGUARIBE', 'TABULEIRO DO NORTE',
    ),
}


def _criar_regioes_saude(cur):
    """macrorregiao (macrorregiões de saúde do Ceará) e municipio_regiao
    (município -> macrorregião), mantidas na tela Cadastros. Na primeira vez
    carrega os 184 municípios de MACRORREGIOES_CE (registrado em migracao).
    Depende de migracao."""
    s = schema()
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {s}.macrorregiao (
                       id    SERIAL PRIMARY KEY,
                       nome  TEXT NOT NULL UNIQUE,
                       ativo BOOLEAN NOT NULL DEFAULT true)""")
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {s}.municipio_regiao (
                       id              SERIAL PRIMARY KEY,
                       macrorregiao_id INTEGER NOT NULL REFERENCES {s}.macrorregiao (id),
                       nome            TEXT NOT NULL UNIQUE,
                       ativo           BOOLEAN NOT NULL DEFAULT true)""")
    cur.execute(f"SELECT 1 FROM {s}.migracao WHERE tabela = 'municipio_regiao'")
    if cur.fetchone():
        return
    linhas = 0
    for macro, municipios in MACRORREGIOES_CE.items():
        cur.execute(f"""INSERT INTO {s}.macrorregiao (nome) VALUES (%s)
                        ON CONFLICT (nome) DO UPDATE SET nome = EXCLUDED.nome RETURNING id""", [macro])
        macro_id = cur.fetchone()['id']
        execute_batch(cur, f'INSERT INTO {s}.municipio_regiao (macrorregiao_id, nome) VALUES (%s, %s) '
                           'ON CONFLICT (nome) DO NOTHING', [(macro_id, m) for m in municipios])
        linhas += len(municipios)
    cur.execute(f"INSERT INTO {s}.migracao (tabela, linhas) VALUES ('municipio_regiao', %s)", [linhas])
    print(f'[painel] macrorregiões de saúde: {linhas} municípios do Ceará')


# Tipos de consulta do sistema hospitalar (t_marcacao.isn_tipo_consulta) que
# vêm com o sistema (versionados no projeto, junto do SQL de apoio; gerados do
# cadastro Tipos de Consulta em 30/10/2026): isn_tipo_consulta;nome
ARQUIVO_TIPOS_CONSULTA = os.path.join(BASE_DIR, 'sql', 'tipos_consulta_padrao.csv')


def tipos_consulta_padrao():
    """{isn_tipo_consulta: nome} do arquivo do sistema; vazio sem ele."""
    if not os.path.exists(ARQUIVO_TIPOS_CONSULTA):
        return {}
    with open(ARQUIVO_TIPOS_CONSULTA, encoding='utf-8', newline='') as f:
        return {int(r['isn_tipo_consulta']): r['nome'].strip() for r in csv.DictReader(f, delimiter=';')}


def _criar_tipos_consulta(cur):
    """tipo_consulta (nome de cada isn_tipo_consulta), mantida na tela
    Cadastros. Na primeira vez carrega o arquivo do sistema (registrado em
    migracao; sem o arquivo, tenta de novo no próximo início). Depende de
    migracao."""
    s = schema()
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {s}.tipo_consulta (
                       id                SERIAL PRIMARY KEY,
                       nome              TEXT NOT NULL UNIQUE,
                       ativo             BOOLEAN NOT NULL DEFAULT true,
                       isn_tipo_consulta INTEGER UNIQUE)""")
    cur.execute(f"SELECT 1 FROM {s}.migracao WHERE tabela = 'tipo_consulta'")
    if cur.fetchone():
        return
    padrao = tipos_consulta_padrao()
    if not padrao:
        print(f'[painel] AVISO: {ARQUIVO_TIPOS_CONSULTA} não encontrado; tipos de consulta sem carga inicial')
        return
    execute_batch(cur, f'INSERT INTO {s}.tipo_consulta (isn_tipo_consulta, nome) VALUES (%s, %s) '
                       'ON CONFLICT DO NOTHING', list(padrao.items()))
    cur.execute(f"INSERT INTO {s}.migracao (tabela, linhas) VALUES ('tipo_consulta', %s)", [len(padrao)])
    print(f'[painel] tipos de consulta: {len(padrao)} cadastrados')


def _criar_tabelas_financeiro(cur):
    """Relatórios do SIAFE importados na tela Lançar Dados do Financeiro
    (financeiro.py): cada importação substitui a anterior do mesmo tipo
    (as linhas saem junto, ON DELETE CASCADE); credor é o cadastro dos
    credores (documento só com dígitos)."""
    s = schema()
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {s}.credor (
                       documento     TEXT PRIMARY KEY,
                       nome          TEXT NOT NULL,
                       atualizado_em TIMESTAMP NOT NULL DEFAULT now())""")
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {s}.importacao_financeiro (
                       id               SERIAL PRIMARY KEY,
                       tipo             TEXT NOT NULL UNIQUE CHECK (tipo IN ('empenhos', 'restos')),
                       arquivo          TEXT NOT NULL,
                       referencia       TEXT,
                       atualizado_siafe TEXT,
                       linhas           INTEGER NOT NULL,
                       totais           JSONB NOT NULL,
                       importado_em     TIMESTAMP NOT NULL DEFAULT now(),
                       importado_por    TEXT NOT NULL)""")
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {s}.empenho_aberto (
                       id                      SERIAL PRIMARY KEY,
                       importacao_id           INTEGER NOT NULL
                                               REFERENCES {s}.importacao_financeiro (id) ON DELETE CASCADE,
                       empenho                 TEXT NOT NULL,
                       fonte                   TEXT,
                       credor_documento        TEXT REFERENCES {s}.credor (documento),
                       valor_a_liquidar        NUMERIC(15, 2) NOT NULL DEFAULT 0,
                       valor_liquidado_a_pagar NUMERIC(15, 2) NOT NULL DEFAULT 0)""")
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {s}.resto_pagar (
                       id               SERIAL PRIMARY KEY,
                       importacao_id    INTEGER NOT NULL
                                        REFERENCES {s}.importacao_financeiro (id) ON DELETE CASCADE,
                       empenho          TEXT NOT NULL,
                       processo         TEXT,
                       fonte            TEXT,
                       contrato         TEXT,
                       credor_documento TEXT REFERENCES {s}.credor (documento),
                       ordem_bancaria   TEXT,
                       data_ob          DATE,
                       valor_inscrito   NUMERIC(15, 2) NOT NULL DEFAULT 0,
                       valor_pago       NUMERIC(15, 2) NOT NULL DEFAULT 0,
                       valor_a_pagar    NUMERIC(15, 2) NOT NULL DEFAULT 0)""")
    # cruzamento dos dois relatórios pelo número do empenho
    cur.execute(f'CREATE INDEX IF NOT EXISTS empenho_aberto_empenho_idx ON {s}.empenho_aberto (empenho)')
    cur.execute(f'CREATE INDEX IF NOT EXISTS resto_pagar_empenho_idx ON {s}.resto_pagar (empenho)')


def nomes_tipos_consulta():
    """{isn_tipo_consulta: nome} dos tipos de consulta ativos no cadastro.
    Enquanto o banco do painel não está pronto ou se a leitura falhar, usa
    o arquivo do sistema (tipos_consulta_padrao)."""
    if pronto():
        try:
            with _cursor() as cur:
                cur.execute(f"""SELECT isn_tipo_consulta, nome FROM {schema()}.tipo_consulta
                                WHERE ativo AND isn_tipo_consulta IS NOT NULL""")
                return {row['isn_tipo_consulta']: row['nome'] for row in cur.fetchall()}
        except Exception as e:
            print(f'[painel] AVISO: cadastro de tipos de consulta indisponível, usando a lista padrão: {e}')
    return tipos_consulta_padrao()


def macrorregiao_dos_municipios():
    """{nome do município (sem acento, maiúsculas): macrorregião} dos
    municípios do Ceará ativos no cadastro. Enquanto o banco do painel não
    está pronto (logo depois de iniciar o servidor) ou se a leitura falhar,
    usa a lista oficial MACRORREGIOES_CE, para os municípios não aparecerem
    todos como não classificados."""
    if pronto():
        try:
            s = schema()
            with _cursor() as cur:
                cur.execute(f"""SELECT m.nome, r.nome AS macro FROM {s}.municipio_regiao m
                                JOIN {s}.macrorregiao r ON r.id = m.macrorregiao_id
                                WHERE m.ativo AND r.ativo""")
                return {_chave_nome(row['nome']): row['macro'] for row in cur.fetchall()}
        except Exception as e:
            print(f'[painel] AVISO: cadastro de macrorregiões indisponível, usando a lista oficial: {e}')
    return {_chave_nome(m): macro for macro, municipios in MACRORREGIOES_CE.items() for m in municipios}


def _criar_controle_migracao(cur):
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.migracao (
            tabela    TEXT PRIMARY KEY,
            feita_em  TIMESTAMP NOT NULL DEFAULT now(),
            linhas    INTEGER NOT NULL
        )""")


def _migracao_feita(tabela):
    with _cursor() as cur:
        cur.execute(f'SELECT 1 FROM {schema()}.migracao WHERE tabela = %s', [tabela.nome])
        return cur.fetchone() is not None


def _registrar_migracao(tabela, linhas):
    with _cursor() as cur:
        cur.execute(f'INSERT INTO {schema()}.migracao (tabela, linhas) VALUES (%s, %s) '
                    'ON CONFLICT (tabela) DO NOTHING', [tabela.nome, linhas])


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
    """Cria as tabelas no schema painel e migra as planilhas uma única vez
    por tabela (registrado em <schema>.migracao: esvaziar uma tabela depois
    não traz os dados das planilhas de volta). Retorna True se o schema está
    pronto para uso."""
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
                _criar_tabelas_unidade(cur)
                _criar_controle_migracao(cur)
                _criar_tabelas_classificacao(cur)
                _carregar_classificacoes_padrao(cur)
                _criar_subcategorias_custo(cur)
                _criar_regioes_saude(cur)
                _criar_tipos_consulta(cur)
                _criar_tabelas_financeiro(cur)
            for t in TABELAS:
                if _migracao_feita(t):
                    continue
                existentes = t.contar()
                if existentes > 0:  # migrada antes de existir o controle
                    _registrar_migracao(t, existentes)
                    continue
                try:
                    qtd = _migrar(t)
                except Exception as e:  # uma planilha com problema não bloqueia as telas
                    print(f'[painel] AVISO: migração de {t.tabela} falhou (tenta de novo no próximo início): {e}')
                    continue
                _registrar_migracao(t, qtd)
                print(f'[painel] {t.tabela}: {qtd} linhas migradas')
            _estado.update(pronto=True, erro=None)
        except Exception as e:
            _estado['erro'] = str(e)
            print(f'[painel] schema dos dados do painel indisponível: {e}')
        return _estado['pronto']


def resumo_tabelas():
    """[{'tabela', 'linhas'}] do schema dos dados do painel, para a tela de
    configuração mostrar o que foi criado (e o que veio das planilhas)."""
    from psycopg2 import sql
    with _cursor() as cur:
        cur.execute("""SELECT table_name FROM information_schema.tables
                       WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name""", [schema()])
        nomes = [r['table_name'] for r in cur.fetchall()]
        resumo = []
        for nome in nomes:
            cur.execute(sql.SQL('SELECT count(*) AS qtd FROM {}.{}').format(sql.Identifier(schema()), sql.Identifier(nome)))
            resumo.append({'tabela': nome, 'linhas': cur.fetchone()['qtd']})
    return resumo


def reiniciar():
    """Depois de mudar a configuração do banco: esquece o estado e tenta de novo."""
    with _lock:
        _estado.update(pronto=False, erro=None, tentativa=None)
    return iniciar(forcar=True)


def iniciar_em_segundo_plano():
    """Tenta iniciar sem segurar quem chamou (início do app e requisições):
    a conexão pode levar até o timeout com o banco fora do ar."""
    tentativa = _estado['tentativa']
    if _lock.locked() or (tentativa is not None and time.monotonic() - tentativa < _INTERVALO_NOVA_TENTATIVA):
        return
    threading.Thread(target=iniciar, name='banco_painel.iniciar', daemon=True).start()


def pronto():
    """True se o schema está pronto. Senão dispara uma nova tentativa em
    segundo plano e responde na hora (os painéis caem para as planilhas)."""
    if _estado['pronto']:
        return True
    iniciar_em_segundo_plano()
    if not _estado['erro']:
        _estado['erro'] = 'Conectando ao banco de dados dos lançamentos; recarregue a página em alguns segundos.'
    return False


def erro():
    return _estado['erro']
