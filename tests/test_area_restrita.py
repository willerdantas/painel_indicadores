"""Login, CSRF, troca obrigatória de senha e ganho de privilégio na área restrita."""
import logos
import paginas_visiveis
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


# ---------------------------------------------------------------- inatividade (15 minutos)

def _ultimo_acesso(cliente, segundos_atras):
    import time
    with cliente.session_transaction() as sess:
        sess['ultimo_acesso'] = time.time() - segundos_atras


def test_sessao_expira_apos_15_minutos_sem_uso(cliente, logar, admin_id):
    logar(admin_id)
    _ultimo_acesso(cliente, 16 * 60)
    r = cliente.get('/admin/usuarios')
    assert r.status_code == 302 and '/login' in r.headers['Location']
    assert 'encerrada após 15 minutos' in cliente.get('/login').get_data(as_text=True)
    with cliente.session_transaction() as sess:
        assert 'usuario_id' not in sess


def test_sessao_continua_com_uso_dentro_do_prazo(cliente, logar, admin_id):
    logar(admin_id)
    _ultimo_acesso(cliente, 14 * 60)
    assert cliente.get('/admin/usuarios').status_code == 200


def test_aviso_de_uso_renova_a_sessao(cliente, logar, admin_id):
    logar(admin_id)
    _ultimo_acesso(cliente, 14 * 60)
    assert cliente.get('/sessao/ativa').status_code == 204
    _ultimo_acesso(cliente, 0)  # renovada: o prazo recomeça
    assert cliente.get('/admin/usuarios').status_code == 200


def test_saida_por_inatividade(cliente, logar, admin_id):
    logar(admin_id)
    r = cliente.get('/logout?inatividade=1&proximo=/admin/usuarios')
    assert r.status_code == 302 and '/login' in r.headers['Location'] and 'proximo=' in r.headers['Location']
    logar(admin_id)
    r = cliente.get('/logout?inatividade=1&proximo=/custos')
    assert r.status_code == 302 and r.headers['Location'].endswith('/custos')  # painel público: fica na página
    logar(admin_id)
    r = cliente.get('/logout?inatividade=1&proximo=//site-externo.com')
    assert '/login' in r.headers['Location'] and 'site-externo' not in r.headers['Location']


def test_cookie_indica_sessao_ativa(cliente, logar, admin_id):
    logar(admin_id)
    r = cliente.get('/admin/usuarios')
    assert 'painel_sessao=1' in ' '.join(r.headers.getlist('Set-Cookie'))
    r = cliente.get('/logout')
    assert any(c.startswith('painel_sessao=;') for c in r.headers.getlist('Set-Cookie'))


# ---------------------------------------------------------------- páginas visíveis (/admin/banco)

def test_todas_as_paginas_visiveis_por_padrao(cliente):
    html = cliente.get('/').get_data(as_text=True)
    for _c, nome, *_ in paginas_visiveis.PAGINAS:
        assert nome in html


def test_pagina_oculta_sai_do_menu_e_nao_abre(cliente, logar, admin_id):
    paginas_visiveis.salvar(['custos', 'lean'])
    html = cliente.get('/').get_data(as_text=True)
    assert 'href="/custos"' not in html and 'Indicadores LEAN' not in html
    assert 'Ambulatório' in html
    assert cliente.get('/custos').status_code == 404
    assert cliente.get('/api/custos/dados').status_code == 404
    assert cliente.get('/lean/tendencias').status_code == 404
    logar(admin_id)  # o administrador ainda abre pelo endereço
    assert cliente.get('/custos').status_code != 404


def test_admin_escolhe_paginas_visiveis(cliente, logar, admin_id):
    logar(admin_id)
    assert 'Páginas visíveis' in cliente.get('/admin/banco').get_data(as_text=True)
    r = cliente.post('/admin/banco/paginas', json={'visiveis': ['custos', 'ambulatorio', 'xpto']},
                     headers={'X-CSRF-Token': 'tok'})
    assert r.get_json()['ok']
    assert paginas_visiveis.ocultas() == set(paginas_visiveis.CHAVES) - {'custos', 'ambulatorio'}


def test_so_admin_escolhe_paginas_visiveis(cliente, logar):
    _, ids = _perfis_e_usuarios()
    logar(ids['caio'])
    r = cliente.post('/admin/banco/paginas', json={'visiveis': []}, headers={'X-CSRF-Token': 'tok'})
    assert r.status_code == 403 and paginas_visiveis.ocultas() == set()


# ---------------------------------------------------------------- logos (/admin/banco)

PNG_MINIMO = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
              b'\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')


def _enviar_logo(cliente, tipo, conteudo, nome='logo.png'):
    import io
    return cliente.post('/admin/banco/logo', data={'tipo': tipo, 'arquivo': (io.BytesIO(conteudo), nome)},
                        headers={'X-CSRF-Token': 'tok'}, content_type='multipart/form-data')


def test_logo_padrao_nas_paginas(cliente):
    html = cliente.get('/').get_data(as_text=True)
    assert 'LOGO%20HM%20E%20SESA' in html or 'LOGO HM E SESA' in html
    assert 'LOGO%20HM%20-%20NOVA' in html or 'LOGO HM - NOVA' in html


def test_admin_troca_e_restaura_logo(cliente, logar, admin_id):
    logar(admin_id)
    r = _enviar_logo(cliente, 'cabecalho', PNG_MINIMO)
    assert r.get_json()['ok'] and '/logo/cabecalho?v=' in r.get_json()['url']
    assert '/logo/cabecalho?v=' in cliente.get('/').get_data(as_text=True)
    img = cliente.get('/logo/cabecalho')
    assert img.status_code == 200 and img.mimetype == 'image/png' and img.data == PNG_MINIMO
    img.close()  # libera o arquivo (no Windows, aberto não pode ser apagado)

    r = cliente.post('/admin/banco/logo/restaurar', json={'tipo': 'cabecalho'}, headers={'X-CSRF-Token': 'tok'})
    assert r.get_json()['ok'] and '/static/' in r.get_json()['url']
    assert logos.arquivo_personalizado('cabecalho') is None


def test_logo_rejeita_arquivo_que_nao_e_imagem(cliente, logar, admin_id):
    logar(admin_id)
    r = _enviar_logo(cliente, 'cabecalho', b'<svg onload="alert(1)"></svg>', 'logo.png')
    assert r.status_code == 400 and logos.arquivo_personalizado('cabecalho') is None
    assert _enviar_logo(cliente, 'outra', PNG_MINIMO).status_code == 400
    grande = PNG_MINIMO + b'0' * logos.TAMANHO_MAXIMO
    assert _enviar_logo(cliente, 'carregando', grande).status_code == 400


def test_so_admin_troca_logo(cliente, logar):
    _, ids = _perfis_e_usuarios()
    logar(ids['caio'])
    assert _enviar_logo(cliente, 'cabecalho', PNG_MINIMO).status_code == 403
    assert logos.arquivo_personalizado('cabecalho') is None


# ---------------------------------------------------------------- servidor novo: senha -> banco

def _admin_provisorio():
    with usuarios._conectar() as c:
        uid = c.execute('SELECT id FROM usuarios').fetchone()[0]
        c.execute('UPDATE usuarios SET trocar_senha = 1, senha_hash = ? WHERE id = ?',
                  [usuarios.generate_password_hash('Provisoria1'), uid])
    return uid


def test_primeiro_acesso_troca_senha_e_vai_para_o_banco(cliente, monkeypatch):
    import area_restrita
    monkeypatch.setattr(area_restrita, '_banco_pendente', lambda: True)
    _admin_provisorio()

    r = cliente.post('/login', data={'usuario': 'admin', 'senha': 'Provisoria1'})
    assert r.status_code == 302
    r = cliente.get('/admin')
    assert r.headers['Location'].endswith('/admin/senha')  # passo 1: senha
    assert 'passo 1 de 2' in cliente.get('/admin/senha').get_data(as_text=True)

    with cliente.session_transaction() as s:
        s['csrf'] = 'tok'
    r = cliente.post('/admin/senha', data={'csrf': 'tok', 'atual': 'Provisoria1',
                                           'nova': 'NovaSenha!9', 'confirmacao': 'NovaSenha!9'})
    assert r.headers['Location'].endswith('/admin/banco')  # passo 2: banco

    # até salvar o banco, o resto da área restrita leva para a configuração
    assert cliente.get('/admin/usuarios').headers['Location'].endswith('/admin/banco')
    assert cliente.post('/admin/cadastros/unidade/salvar', json={}, headers={'X-CSRF-Token': 'tok'}).status_code == 409
    html = cliente.get('/admin/banco').get_data(as_text=True)
    assert 'passo 2 de 2' in html
    for nome in ('Indicadores LEAN', 'Exames', 'Ambulatório', 'Indicadores Hospitalares', 'Indicadores da Emergência'):
        assert nome in html
    assert 'Dados do painel' in html and 'importação de planilhas' in html


def test_com_banco_configurado_senha_volta_ao_menu(cliente):
    _admin_provisorio()
    cliente.post('/login', data={'usuario': 'admin', 'senha': 'Provisoria1'})
    with cliente.session_transaction() as s:
        s['csrf'] = 'tok'
    r = cliente.post('/admin/senha', data={'csrf': 'tok', 'atual': 'Provisoria1',
                                           'nova': 'NovaSenha!9', 'confirmacao': 'NovaSenha!9'})
    assert r.headers['Location'].endswith('/admin')
    assert 'passo 2 de 2' not in cliente.get('/admin/banco').get_data(as_text=True)


def test_banco_pendente_nao_desvia_quem_nao_e_admin(cliente, logar, monkeypatch):
    import area_restrita
    _, ids = _perfis_e_usuarios()
    monkeypatch.setattr(area_restrita, '_banco_pendente', lambda: True)
    logar(ids['bia'])
    assert cliente.get('/admin/usuarios').status_code == 200


def test_teste_de_conexao_cobre_todos_os_paineis():
    import config_banco
    nomes = [n for n, _t in config_banco.MODULOS_HOSPITALAR]
    assert nomes == ['Indicadores LEAN', 'Indicadores Hospitalares', 'Indicadores da Emergência', 'Exames', 'Ambulatório']
    for t in ('t_marcacao', 't_especialidade', 't_marcacao_situacao', 't_exame_solic', 't_internacao_leito'):
        assert t in config_banco.TABELAS_USADAS


def test_painel_sem_banco_configurado_mostra_aviso(cliente, monkeypatch):
    import config_banco
    import painel

    def sem_banco():
        raise config_banco.ConfiguracaoAusente('O banco de dados ainda não foi configurado.')
    monkeypatch.setattr(painel, 'hcasg_get_filtros', sem_banco)
    r = cliente.get('/hcasg/hospitalares')
    assert r.status_code == 503 and 'ainda não foi configurado' in r.get_data(as_text=True)


# ---------------------------------------------------------------- administrador inicial sem senha

def test_admin_inicial_entra_sem_senha_so_no_primeiro_acesso(cliente):
    assert usuarios.sem_senha(usuarios.listar_usuarios()[0]['id'])
    assert usuarios.autenticar('admin', '') is not None
    assert usuarios.autenticar('admin', 'qualquer') is None

    r = cliente.post('/login', data={'usuario': 'admin', 'senha': ''})
    assert r.status_code == 302
    html = cliente.get('/admin/senha').get_data(as_text=True)
    assert 'id="atual"' not in html  # sem senha atual para informar

    with cliente.session_transaction() as s:
        s['csrf'] = 'tok'
    r = cliente.post('/admin/senha', data={'csrf': 'tok', 'atual': '', 'nova': 'NovaSenha!9',
                                           'confirmacao': 'NovaSenha!9'})
    assert r.status_code == 302
    assert usuarios.autenticar('admin', '') is None  # em branco não entra mais
    assert usuarios.autenticar('admin', 'NovaSenha!9') is not None


def test_sem_senha_nao_vale_depois_de_liberada_a_troca(admin_id):
    # admin_id já trocou a senha; outro usuário sem senha e sem troca pendente não entra
    with usuarios._conectar() as c:
        c.execute('UPDATE usuarios SET senha_hash = ?, trocar_senha = 0 WHERE id = ?', [usuarios.SEM_SENHA, admin_id])
    assert usuarios.autenticar('admin', '') is None


def test_admin_pass_define_a_senha_inicial(tmp_path, monkeypatch):
    monkeypatch.setattr(usuarios, 'ARQUIVO_DB', str(tmp_path / 'outro.db'))
    monkeypatch.setenv('ADMIN_PASS', 'DefinidaNoEnv1')
    usuarios.iniciar()
    assert usuarios.autenticar('admin', '') is None
    assert usuarios.autenticar('admin', 'DefinidaNoEnv1') is not None


# ---------------------------------------------------------------- login sem banco configurado

def test_sem_banco_admin_entra_sem_senha_mesmo_com_senha_salva(cliente, admin_id, monkeypatch):
    import area_restrita
    monkeypatch.setattr(area_restrita, '_banco_pendente', lambda: True)
    html = cliente.get('/login').get_data(as_text=True)
    assert 'Banco de dados ainda não configurado' in html

    r = cliente.post('/login', data={'usuario': 'admin', 'senha': ''})
    assert r.status_code == 302
    assert cliente.get('/admin').headers['Location'].endswith('/admin/senha')  # define a senha primeiro
    assert 'id="atual"' not in cliente.get('/admin/senha').get_data(as_text=True)


def test_sem_banco_so_admin_entra_sem_senha(cliente, monkeypatch):
    import area_restrita
    _perfis_e_usuarios()
    monkeypatch.setattr(area_restrita, '_banco_pendente', lambda: True)
    assert cliente.post('/login', data={'usuario': 'caio', 'senha': ''}).status_code == 200
    assert cliente.post('/login', data={'usuario': 'ninguem', 'senha': ''}).status_code == 200


def test_com_banco_configurado_senha_em_branco_nao_entra(cliente, admin_id):
    assert 'Banco de dados ainda não configurado' not in cliente.get('/login').get_data(as_text=True)
    assert cliente.post('/login', data={'usuario': 'admin', 'senha': ''}).status_code == 200
    assert usuarios.autenticar('admin', 'SenhaAdmin1') is not None  # senha intacta


# ---------------------------------------------------------------- classificação de exames

def test_classificacoes_ficam_em_cadastros(cliente, logar, admin_id):
    assert 'classificacoes' not in usuarios.CHAVES_PAGINAS  # permissão é a de Cadastros
    assert usuarios.pagina_da_rota('/admin/classificacoes/associar') == 'cadastros'
    logar(admin_id)
    assert 'Classificação de Exames' not in cliente.get('/admin').get_data(as_text=True)  # sem cartão próprio
    r = cliente.get('/admin/classificacoes')  # endereço antigo: vai para a seção em Cadastros
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/cadastros#classificacao-exames')


def test_classificacoes_bloqueada_sem_permissao(cliente, logar):
    _, ids = _perfis_e_usuarios()
    logar(ids['caio'])  # perfil Amplo: perfis, custos, simaq
    assert cliente.get('/admin/classificacoes').status_code == 403
    r = cliente.post('/admin/classificacoes/associar', json={'mudancas': {}}, headers={'X-CSRF-Token': 'tok'})
    assert r.status_code == 403


def test_associar_rejeita_dados_invalidos(cliente, logar, admin_id):
    logar(admin_id)
    r = cliente.post('/admin/classificacoes/associar', json={'mudancas': [1, 2]}, headers={'X-CSRF-Token': 'tok'})
    assert r.status_code == 400


def test_exames_sem_schema_do_painel_ficam_sem_classificacao():
    import classificacao_exames
    assert 'WHERE false' in classificacao_exames.subconsulta_sql()
    assert classificacao_exames.nomes() == []


def test_classificacao_saiu_do_banco_hospitalar():
    import config_banco
    assert 't_exame_classificacao' not in config_banco.TABELAS_USADAS
