"""Login, CSRF, troca obrigatória de senha e ganho de privilégio na área restrita."""
import usuarios


def _perfis_e_usuarios():
    """SoPerfis (perfis), SoUsuarios (usuarios) e Amplo (perfis, custos, simaq),
    com um usuário em cada: ana, bia e caio."""
    assert usuarios.salvar_perfil(None, 'SoPerfis', False, ['perfis']) is None
    assert usuarios.salvar_perfil(None, 'SoUsuarios', False, ['usuarios']) is None
    assert usuarios.salvar_perfil(None, 'Amplo', False, ['perfis', 'custos', 'simaq']) is None
    perfis = {p['nome']: p['id'] for p in usuarios.listar_perfis()}
    for login, perfil in (('ana', 'SoPerfis'), ('bia', 'SoUsuarios'), ('caio', 'Amplo')):
        assert usuarios.salvar_usuario(None, login, login.title(), 'senha123', perfis[perfil], True) is None
    with usuarios._conectar() as c:
        c.execute('UPDATE usuarios SET trocar_senha = 0')
    ids = {u['login']: u['id'] for u in usuarios.listar_usuarios()}
    return perfis, ids


# ---------------------------------------------------------------- admin inicial e senha

def test_admin_inicial_nao_usa_senha_padrao():
    assert usuarios.autenticar('admin', 'admin') is None
    with usuarios._conectar() as c:
        assert c.execute('SELECT trocar_senha FROM usuarios').fetchone()[0] == 1


def test_senha_admin_antiga_obriga_troca():
    with usuarios._conectar() as c:
        c.execute('UPDATE usuarios SET trocar_senha = 0, senha_hash = ?',
                  [usuarios.generate_password_hash('admin')])
    usuarios.iniciar()
    with usuarios._conectar() as c:
        assert c.execute('SELECT trocar_senha FROM usuarios').fetchone()[0] == 1


def test_troca_obrigatoria_redireciona_ate_trocar(cliente, logar):
    with usuarios._conectar() as c:
        uid = c.execute('SELECT id FROM usuarios').fetchone()[0]
        c.execute('UPDATE usuarios SET senha_hash = ? WHERE id = ?',
                  [usuarios.generate_password_hash('provisoria1'), uid])
    logar(uid)
    r = cliente.get('/admin/usuarios')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/senha')
    assert cliente.get('/admin/senha').status_code == 200

    r = cliente.post('/admin/senha', data={'csrf': 'tok', 'atual': 'provisoria1',
                                           'nova': 'NovaSenha!9', 'confirmacao': 'NovaSenha!9'})
    assert r.status_code == 302
    assert cliente.get('/admin/usuarios').status_code == 200
    assert usuarios.autenticar('admin', 'NovaSenha!9')


def test_trocar_senha_valida_atual_e_confirmacao(admin_id):
    assert usuarios.trocar_senha(admin_id, 'errada', 'OutraSenha1', 'OutraSenha1') == 'Senha atual incorreta.'
    assert 'confirmação' in usuarios.trocar_senha(admin_id, 'SenhaAdmin1', 'OutraSenha1', 'Outra')
    assert '8 caracteres' in usuarios.trocar_senha(admin_id, 'SenhaAdmin1', 'curta', 'curta')
    assert usuarios.trocar_senha(admin_id, 'SenhaAdmin1', 'OutraSenha1', 'OutraSenha1') is None


def test_senha_dada_por_outra_pessoa_e_provisoria(cliente, logar, admin_id):
    perfis, _ = _perfis_e_usuarios()
    logar(admin_id)
    cliente.post('/admin/usuarios/salvar', data={'csrf': 'tok', 'login': 'dani', 'nome': 'Dani',
                                                 'senha': 'senha123', 'perfil_id': perfis['SoUsuarios'],
                                                 'ativo': '1'})
    dani = next(u for u in usuarios.listar_usuarios() if u['login'] == 'dani')
    assert usuarios.obter_usuario(dani['id'])['trocar_senha']


# ---------------------------------------------------------------- login

def test_login_bloqueia_depois_de_varios_erros(cliente, admin_id):
    for _ in range(5):
        assert cliente.post('/login', data={'usuario': 'admin', 'senha': 'errada'}).status_code == 200
    r = cliente.post('/login', data={'usuario': 'admin', 'senha': 'SenhaAdmin1'})
    assert r.status_code == 429  # nem a senha certa entra enquanto durar o bloqueio


def test_login_certo_redireciona(cliente, admin_id):
    r = cliente.post('/login', data={'usuario': 'admin', 'senha': 'SenhaAdmin1'})
    assert r.status_code == 302


def test_arquivo_estatico_nao_consulta_usuario(cliente, logar, admin_id, monkeypatch):
    logar(admin_id)

    def falhar(_id):
        raise AssertionError('consultou o usuário num arquivo estático')
    monkeypatch.setattr(usuarios, 'obter_usuario', falhar)
    assert cliente.get('/static/nao-existe.png').status_code == 404


# ---------------------------------------------------------------- CSRF

def test_csrf_post_sem_corpo(cliente, logar, admin_id):
    logar(admin_id)
    assert cliente.post('/admin/usuarios/99/excluir').status_code == 400


def test_csrf_json_exige_header(cliente, logar, admin_id):
    logar(admin_id)
    assert cliente.post('/admin/banco/testar', json={}).status_code == 400
    assert cliente.post('/admin/banco/testar', json={}, headers={'X-CSRF-Token': 'errado'}).status_code == 400
    r = cliente.post('/admin/banco/testar', json={}, headers={'X-CSRF-Token': 'tok'})
    assert 'obrigatório' in r.get_json()['erro']  # passou do CSRF e caiu na validação dos campos


# ---------------------------------------------------------------- ganho de privilégio

def test_nao_edita_o_proprio_perfil(cliente, logar):
    perfis, ids = _perfis_e_usuarios()
    logar(ids['ana'])
    cliente.post('/admin/perfis/salvar', data={'csrf': 'tok', 'id': perfis['SoPerfis'], 'nome': 'SoPerfis',
                                               'paginas': ['perfis', 'custos', 'usuarios']})
    assert usuarios.obter_perfil(perfis['SoPerfis'])['paginas'] == ['perfis']


def test_nao_libera_pagina_que_nao_tem(cliente, logar):
    _, ids = _perfis_e_usuarios()
    logar(ids['ana'])
    cliente.post('/admin/perfis/salvar', data={'csrf': 'tok', 'nome': 'Novo', 'paginas': ['perfis', 'simaq']})
    assert 'Novo' not in [p['nome'] for p in usuarios.listar_perfis()]


def test_nao_edita_perfil_mais_amplo(cliente, logar):
    perfis, ids = _perfis_e_usuarios()
    logar(ids['ana'])
    cliente.post('/admin/perfis/salvar', data={'csrf': 'tok', 'id': perfis['Amplo'], 'nome': 'Amplo',
                                               'paginas': ['perfis']})
    assert 'custos' in usuarios.obter_perfil(perfis['Amplo'])['paginas']


def test_nao_muda_o_proprio_perfil_de_usuario(cliente, logar):
    perfis, ids = _perfis_e_usuarios()
    logar(ids['bia'])
    cliente.post('/admin/usuarios/salvar', data={'csrf': 'tok', 'id': ids['bia'], 'login': 'bia', 'nome': 'Bia',
                                                 'perfil_id': perfis['Amplo'], 'ativo': '1'})
    assert usuarios.obter_usuario(ids['bia'])['perfil_id'] == perfis['SoUsuarios']


def test_nao_troca_senha_de_usuario_mais_amplo(cliente, logar):
    perfis, ids = _perfis_e_usuarios()
    logar(ids['bia'])
    cliente.post('/admin/usuarios/salvar', data={'csrf': 'tok', 'id': ids['caio'], 'login': 'caio', 'nome': 'Caio',
                                                 'senha': 'roubada1', 'perfil_id': perfis['Amplo'], 'ativo': '1'})
    assert usuarios.autenticar('caio', 'roubada1') is None


def test_admin_continua_podendo_tudo(cliente, logar, admin_id):
    perfis, _ = _perfis_e_usuarios()
    logar(admin_id)
    cliente.post('/admin/perfis/salvar', data={'csrf': 'tok', 'id': perfis['SoPerfis'], 'nome': 'SoPerfis',
                                               'paginas': ['perfis', 'custos']})
    assert usuarios.obter_perfil(perfis['SoPerfis'])['paginas'] == ['perfis', 'custos']
