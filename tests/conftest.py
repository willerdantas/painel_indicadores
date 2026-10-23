"""Ambiente dos testes: SQLite de usuários temporário e sem PostgreSQL.

O app é importado uma vez por sessão; cada teste começa com um banco de
usuários vazio (só o administrador inicial)."""
import os
import sys
import tempfile

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
os.environ.pop('ADMIN_PASS', None)
os.environ.setdefault('FLASK_SECRET_KEY', 'teste')

import banco_painel  # noqa: E402
import paginas_visiveis  # noqa: E402
import usuarios  # noqa: E402

# sem PostgreSQL: os painéis caem para as planilhas e as telas de lançamento
# mostram "indisponível"
banco_painel.iniciar = lambda forcar=False: False
banco_painel.iniciar_em_segundo_plano = lambda: None
banco_painel.pronto = lambda: False

_ARQUIVO_DB_REAL = usuarios.ARQUIVO_DB
usuarios.ARQUIVO_DB = os.path.join(tempfile.mkdtemp(), 'usuarios_import.db')

import painel  # noqa: E402
import area_restrita  # noqa: E402


@pytest.fixture(autouse=True)
def banco_usuarios(tmp_path, monkeypatch):
    monkeypatch.setattr(usuarios, 'ARQUIVO_DB', str(tmp_path / 'usuarios.db'))
    monkeypatch.setattr(paginas_visiveis, 'ARQUIVO', str(tmp_path / 'config_paginas.json'))
    usuarios.iniciar()
    area_restrita._login_falhas.clear()
    assert usuarios.ARQUIVO_DB != _ARQUIVO_DB_REAL


@pytest.fixture
def app():
    painel.app.config['TESTING'] = True
    return painel.app


@pytest.fixture
def cliente(app):
    return app.test_client()


@pytest.fixture
def logar(cliente):
    """logar(usuario_id): sessão autenticada com o token CSRF 'tok'."""
    def _logar(usuario_id):
        with cliente.session_transaction() as s:
            s.clear()
            s['usuario_id'] = usuario_id
            s['csrf'] = 'tok'
    return _logar


@pytest.fixture
def admin_id():
    """Administrador inicial, já com a senha trocada."""
    with usuarios._conectar() as c:
        r = c.execute('SELECT id FROM usuarios').fetchone()
        c.execute('UPDATE usuarios SET trocar_senha = 0, senha_hash = ? WHERE id = ?',
                  [usuarios.generate_password_hash('SenhaAdmin1'), r['id']])
    return r['id']
