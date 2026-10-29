"""
usuarios.py

Usuários e perfis de acesso do painel.

Ficam num SQLite local (painel_usuarios.db, fora do git), e não no PostgreSQL
do hospital: o painel só precisa ler o banco de produção.

Os painéis são públicos; o login é só para a área restrita.
- Perfil administrador: tudo da área restrita, inclusive a configuração do
  banco (exclusiva do administrador).
- Demais perfis: só as áreas restritas marcadas no perfil.

Na primeira execução é criado o perfil "Administrador" e um usuário com o
login ADMIN_USER (padrão admin) e a senha ADMIN_PASS; sem ADMIN_PASS a senha
é gerada e mostrada no console. Senha inicial, senha definida por outra
pessoa e a antiga senha padrão "admin" obrigam a troca no próximo acesso.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# arquivos locais (chaves, configuração, SQLite): PAINEL_PASTA_LOCAL ou a pasta do projeto
PASTA_LOCAL = os.environ.get('PAINEL_PASTA_LOCAL') or BASE_DIR
ARQUIVO_DB = os.path.join(PASTA_LOCAL, 'painel_usuarios.db')

# Áreas restritas que podem ser liberadas por perfil: chave, nome e os
# prefixos de rota que pertencem a ela. Os painéis são públicos e não entram
# aqui; a configuração do banco é exclusiva do administrador (não é marcável).
# Cada nova tela de lançamento de dados entra nesta lista.
PAGINAS = [
    ('usuarios', 'Usuários', ['/admin/usuarios']),
    ('perfis', 'Perfis de Acesso', ['/admin/perfis']),
    ('custos', 'Lançar Dados de Custos', ['/admin/custos']),
    ('producao', 'Lançar Dados de Produção', ['/admin/producao']),
    ('simaq', 'Lançar Dados do SIMAQ', ['/admin/simaq']),
    # inclui a Classificação de Exames, que é uma seção da tela Cadastros
    ('cadastros', 'Cadastros', ['/admin/cadastros', '/admin/classificacoes']),
]
CHAVES_PAGINAS = [p[0] for p in PAGINAS]

_lock = threading.Lock()

# senha_hash do administrador inicial sem senha (ADMIN_PASS não informado):
# entra com a senha em branco só enquanto não definir uma (trocar_senha = 1)
SEM_SENHA = ''


def _conectar():
    conn = sqlite3.connect(ARQUIVO_DB)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def iniciar():
    """Cria as tabelas e, se não houver usuário nenhum, o administrador inicial."""
    with _lock, _conectar() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS perfis (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                nome    TEXT NOT NULL UNIQUE,
                admin   INTEGER NOT NULL DEFAULT 0,
                paginas TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS usuarios (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                login      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                nome       TEXT NOT NULL,
                senha_hash TEXT NOT NULL,
                perfil_id  INTEGER NOT NULL REFERENCES perfis(id),
                ativo      INTEGER NOT NULL DEFAULT 1,
                criado_em  TEXT NOT NULL
            );
        """)
        colunas = [c['name'] for c in conn.execute('PRAGMA table_info(usuarios)')]
        if 'trocar_senha' not in colunas:
            conn.execute('ALTER TABLE usuarios ADD COLUMN trocar_senha INTEGER NOT NULL DEFAULT 0')
        if conn.execute('SELECT COUNT(*) FROM usuarios').fetchone()[0] == 0:
            perfil = conn.execute("SELECT id FROM perfis WHERE admin = 1 ORDER BY id LIMIT 1").fetchone()
            if perfil is None:
                cur = conn.execute("INSERT INTO perfis (nome, admin, paginas) VALUES ('Administrador', 1, ?)",
                                   [json.dumps(CHAVES_PAGINAS)])
                perfil_id = cur.lastrowid
            else:
                perfil_id = perfil['id']
            login = os.environ.get('ADMIN_USER', 'admin')
            senha = os.environ.get('ADMIN_PASS')
            if senha:
                senha_hash = generate_password_hash(senha)
            else:
                # servidor novo: o primeiro acesso é sem senha e a troca é obrigatória
                senha_hash = SEM_SENHA
                print(f'[painel] Administrador inicial criado: login "{login}", sem senha '
                      '(defina a senha no primeiro acesso).', flush=True)
            conn.execute(
                'INSERT INTO usuarios (login, nome, senha_hash, perfil_id, ativo, criado_em, trocar_senha) '
                'VALUES (?, ?, ?, ?, 1, ?, 1)',
                [login, 'Administrador', senha_hash,
                 perfil_id, datetime.now().isoformat(timespec='seconds')])
        # instalações antigas: quem ainda usa a senha padrão "admin" tem de trocar
        for r in conn.execute('SELECT id, senha_hash FROM usuarios WHERE trocar_senha = 0').fetchall():
            if check_password_hash(r['senha_hash'], 'admin'):
                conn.execute('UPDATE usuarios SET trocar_senha = 1 WHERE id = ?', [r['id']])


# ------------------------------------------------------------------ perfis

def _perfil_dict(r):
    return {'id': r['id'], 'nome': r['nome'], 'admin': bool(r['admin']),
            'paginas': [p for p in json.loads(r['paginas'] or '[]') if p in CHAVES_PAGINAS]}


def listar_perfis():
    with _conectar() as conn:
        rows = conn.execute("""
            SELECT p.*, (SELECT COUNT(*) FROM usuarios u WHERE u.perfil_id = p.id) AS qtd_usuarios
            FROM perfis p ORDER BY p.admin DESC, p.nome
        """).fetchall()
    return [{**_perfil_dict(r), 'qtd_usuarios': r['qtd_usuarios']} for r in rows]


def obter_perfil(perfil_id):
    with _conectar() as conn:
        r = conn.execute('SELECT * FROM perfis WHERE id = ?', [perfil_id]).fetchone()
    return _perfil_dict(r) if r else None


def salvar_perfil(perfil_id, nome, admin, paginas):
    """Cria (perfil_id None) ou atualiza um perfil. Retorna mensagem de erro ou None."""
    nome = (nome or '').strip()
    if not nome:
        return 'Informe o nome do perfil.'
    paginas = [p for p in paginas if p in CHAVES_PAGINAS]
    with _lock, _conectar() as conn:
        if conn.execute('SELECT 1 FROM perfis WHERE nome = ? AND id IS NOT ?', [nome, perfil_id]).fetchone():
            return 'Já existe um perfil com esse nome.'
        if perfil_id is not None and not admin and _ultimo_admin(conn, perfil_id=perfil_id):
            return 'Este é o único perfil administrador com usuários ativos; ele não pode deixar de ser administrador.'
        if perfil_id is None:
            conn.execute('INSERT INTO perfis (nome, admin, paginas) VALUES (?, ?, ?)',
                         [nome, int(bool(admin)), json.dumps(paginas)])
        else:
            conn.execute('UPDATE perfis SET nome = ?, admin = ?, paginas = ? WHERE id = ?',
                         [nome, int(bool(admin)), json.dumps(paginas), perfil_id])
    return None


def excluir_perfil(perfil_id):
    with _lock, _conectar() as conn:
        if conn.execute('SELECT 1 FROM usuarios WHERE perfil_id = ?', [perfil_id]).fetchone():
            return 'Há usuários com este perfil; troque o perfil deles antes de excluir.'
        conn.execute('DELETE FROM perfis WHERE id = ?', [perfil_id])
    return None


def _ultimo_admin(conn, perfil_id=None, usuario_id=None):
    """True se a mudança deixaria o sistema sem nenhum administrador ativo."""
    admins = conn.execute("""
        SELECT u.id, u.perfil_id FROM usuarios u JOIN perfis p ON p.id = u.perfil_id
        WHERE p.admin = 1 AND u.ativo = 1
    """).fetchall()
    restantes = [a for a in admins
                 if (perfil_id is None or a['perfil_id'] != perfil_id)
                 and (usuario_id is None or a['id'] != usuario_id)]
    return len(admins) > 0 and len(restantes) == 0


# ------------------------------------------------------------------ usuários

def _usuario_dict(r):
    return {'id': r['id'], 'login': r['login'], 'nome': r['nome'], 'perfil_id': r['perfil_id'],
            'ativo': bool(r['ativo']), 'criado_em': r['criado_em'], 'trocar_senha': bool(r['trocar_senha'])}


def listar_usuarios():
    with _conectar() as conn:
        rows = conn.execute("""
            SELECT u.*, p.nome AS perfil_nome, p.admin AS perfil_admin
            FROM usuarios u JOIN perfis p ON p.id = u.perfil_id
            ORDER BY u.nome
        """).fetchall()
    return [{**_usuario_dict(r), 'perfil_nome': r['perfil_nome'], 'perfil_admin': bool(r['perfil_admin'])}
            for r in rows]


def obter_usuario(usuario_id):
    """Usuário com o perfil já carregado (usado a cada requisição)."""
    with _conectar() as conn:
        r = conn.execute('SELECT * FROM usuarios WHERE id = ?', [usuario_id]).fetchone()
        if r is None:
            return None
        p = conn.execute('SELECT * FROM perfis WHERE id = ?', [r['perfil_id']]).fetchone()
    return {**_usuario_dict(r), 'perfil': _perfil_dict(p) if p else None}


def autenticar(login, senha):
    """Retorna o usuário se login/senha conferem e ele está ativo; senão None."""
    with _conectar() as conn:
        r = conn.execute('SELECT * FROM usuarios WHERE login = ?', [(login or '').strip()]).fetchone()
    if r is None or not r['ativo'] or not _senha_confere(r, senha):
        return None
    return obter_usuario(r['id'])


def _senha_confere(r, senha):
    if r['senha_hash'] == SEM_SENHA:
        return not senha and bool(r['trocar_senha'])  # primeiro acesso sem senha
    return check_password_hash(r['senha_hash'], senha or '')


def liberar_admin_sem_senha(login):
    """Servidor sem banco configurado: o administrador informado entra com a
    senha em branco, para configurar o banco. A senha dele é apagada e a troca
    fica obrigatória (passo 1 do primeiro acesso). Retorna o usuário ou None."""
    with _lock, _conectar() as conn:
        r = conn.execute("""SELECT u.id FROM usuarios u JOIN perfis p ON p.id = u.perfil_id
                           WHERE u.login = ? AND u.ativo = 1 AND p.admin = 1""", [(login or '').strip()]).fetchone()
        if r is None:
            return None
        conn.execute('UPDATE usuarios SET senha_hash = ?, trocar_senha = 1 WHERE id = ?', [SEM_SENHA, r['id']])
    return obter_usuario(r['id'])


def sem_senha(usuario_id):
    """True se o usuário ainda não tem senha (primeiro acesso do administrador)."""
    with _conectar() as conn:
        r = conn.execute('SELECT senha_hash FROM usuarios WHERE id = ?', [usuario_id]).fetchone()
    return r is not None and r['senha_hash'] == SEM_SENHA


def salvar_usuario(usuario_id, login, nome, senha, perfil_id, ativo, trocar_senha=True):
    """Cria (usuario_id None) ou atualiza. Senha em branco na edição mantém a atual.
    trocar_senha: a senha informada é provisória (definida por outra pessoa)."""
    login, nome = (login or '').strip(), (nome or '').strip()
    if not login or not nome:
        return 'Informe login e nome.'
    if usuario_id is None and not senha:
        return 'Informe a senha do novo usuário.'
    if senha and len(senha) < 6:
        return 'A senha deve ter pelo menos 6 caracteres.'
    with _lock, _conectar() as conn:
        perfil = conn.execute('SELECT * FROM perfis WHERE id = ?', [perfil_id]).fetchone()
        if perfil is None:
            return 'Escolha um perfil válido.'
        if conn.execute('SELECT 1 FROM usuarios WHERE login = ? AND id IS NOT ?', [login, usuario_id]).fetchone():
            return 'Já existe um usuário com esse login.'
        if usuario_id is not None and (not ativo or not perfil['admin']) and _ultimo_admin(conn, usuario_id=usuario_id):
            return 'Este é o único administrador ativo; ele não pode ser desativado nem perder o perfil de administrador.'
        if usuario_id is None:
            conn.execute(
                'INSERT INTO usuarios (login, nome, senha_hash, perfil_id, ativo, criado_em, trocar_senha) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                [login, nome, generate_password_hash(senha), perfil_id, int(bool(ativo)),
                 datetime.now().isoformat(timespec='seconds'), int(bool(trocar_senha))])
        else:
            conn.execute('UPDATE usuarios SET login = ?, nome = ?, perfil_id = ?, ativo = ? WHERE id = ?',
                         [login, nome, perfil_id, int(bool(ativo)), usuario_id])
            if senha:
                conn.execute('UPDATE usuarios SET senha_hash = ?, trocar_senha = ? WHERE id = ?',
                             [generate_password_hash(senha), int(bool(trocar_senha)), usuario_id])
    return None


def trocar_senha(usuario_id, atual, nova, confirmacao):
    """Troca a própria senha. Retorna mensagem de erro ou None."""
    if not nova or len(nova) < 8:
        return 'A nova senha deve ter pelo menos 8 caracteres.'
    if nova != confirmacao:
        return 'A confirmação não confere com a nova senha.'
    if nova == atual or nova.lower() == 'admin':
        return 'Escolha uma senha diferente da atual.'
    with _lock, _conectar() as conn:
        r = conn.execute('SELECT senha_hash, trocar_senha FROM usuarios WHERE id = ?', [usuario_id]).fetchone()
        if r is None or not _senha_confere(r, atual):
            return 'Senha atual incorreta.'
        conn.execute('UPDATE usuarios SET senha_hash = ?, trocar_senha = 0 WHERE id = ?',
                     [generate_password_hash(nova), usuario_id])
    return None


def excluir_usuario(usuario_id):
    with _lock, _conectar() as conn:
        if _ultimo_admin(conn, usuario_id=usuario_id):
            return 'Este é o único administrador ativo; não pode ser excluído.'
        conn.execute('DELETE FROM usuarios WHERE id = ?', [usuario_id])
    return None


# ------------------------------------------------------------------ acesso

def pagina_da_rota(caminho):
    """Chave da página do sistema a que a rota pertence, ou None."""
    for chave, _, prefixos in PAGINAS:
        for p in prefixos:
            if caminho == p or caminho.startswith(p + '/'):
                return chave
    return None


def pode_acessar(usuario, chave_pagina):
    if not usuario or not usuario.get('ativo') or not usuario.get('perfil'):
        return False
    perfil = usuario['perfil']
    return perfil['admin'] or chave_pagina in perfil['paginas']


def eh_admin(usuario):
    return bool(usuario and usuario.get('ativo') and usuario.get('perfil') and usuario['perfil']['admin'])
