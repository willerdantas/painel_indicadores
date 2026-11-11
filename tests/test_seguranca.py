"""Cabeçalhos de segurança, HTTPS opcional, validação de parâmetros, erros sem
detalhes técnicos, limite de envio, armadilha de robôs no login e planilhas
conferidas pelo conteúdo."""
import io

import financeiro
import painel
import seguranca
import usuarios


# ---------------------------------------------------------------- cabeçalhos

def test_cabecalhos_de_seguranca_nos_paineis(cliente):
    r = cliente.get('/')
    assert r.headers['X-Content-Type-Options'] == 'nosniff'
    assert "object-src 'none'" in r.headers['Content-Security-Policy']
    assert 'frame-ancestors' not in r.headers['Content-Security-Policy']  # painéis podem ser embutidos
    assert r.headers['Referrer-Policy'] == 'strict-origin-when-cross-origin'
    assert 'Strict-Transport-Security' not in r.headers  # HTTPS desligado
    assert "'unsafe-eval'" not in r.headers['Content-Security-Policy']
    # só as páginas com Plotly 1.x (que usa eval ao carregar) liberam eval
    assert "script-src 'self' 'unsafe-inline' 'unsafe-eval'" in seguranca.CSP_PLOTLY


def test_login_e_area_restrita_nao_abrem_em_iframe_nem_ficam_em_cache(cliente, logar, admin_id):
    r = cliente.get('/login')
    assert r.headers['X-Frame-Options'] == 'DENY'
    assert "frame-ancestors 'none'" in r.headers['Content-Security-Policy']
    assert r.headers['Cache-Control'] == 'no-store'
    logar(admin_id)
    assert cliente.get('/admin/usuarios').headers['X-Frame-Options'] == 'DENY'


def test_saude_e_robots(cliente):
    assert cliente.get('/saude').get_data(as_text=True) == 'ok'
    assert 'Disallow: /' in cliente.get('/robots.txt').get_data(as_text=True)


def test_https_ligado_redireciona_e_envia_hsts(cliente, monkeypatch):
    monkeypatch.setattr(seguranca, 'HTTPS', True)
    r = cliente.get('/login?proximo=/admin')
    assert r.status_code == 301 and r.headers['Location'].startswith('https://')
    assert cliente.get('/saude').status_code == 200  # healthcheck do container continua em http
    r = cliente.get('/login', base_url='https://localhost')
    assert r.status_code == 200 and 'max-age=' in r.headers['Strict-Transport-Security']


# ---------------------------------------------------------------- parâmetros e erros

def test_parametro_de_periodo_invalido_responde_400_sem_consultar(cliente, monkeypatch):
    chamado = []
    monkeypatch.setattr(painel, 'get_indicadores_hospitalar', lambda *a: chamado.append(a) or {})
    for consulta in ('data_ini=2026-13-01&data_fim=2026-10-31',
                     "data_ini=2026-10-01'; DROP TABLE x;--&data_fim=2026-10-31",
                     'data_ini=2026-10-01&data_fim=2026-10-31&clinica=' + 'x' * 600):
        r = cliente.get('/api/hcasg/hospitalares/indicadores?' + consulta)
        assert r.status_code == 400 and 'Parâmetro inválido' in r.get_json()['erro']
    assert not chamado
    assert cliente.get('/api/custos/dados?mes_ini=abc').status_code == 400
    assert cliente.get('/api/hcasg/emergencia/atendimentos?hora_ini=x').status_code == 400

    r = cliente.get('/api/hcasg/hospitalares/indicadores?data_ini=2026-10-01&data_fim=2026-10-31')
    assert r.status_code == 200 and chamado


def test_erro_interno_nao_mostra_detalhes_tecnicos(cliente, monkeypatch, capsys):
    def falha():
        raise RuntimeError('relation "t_segredo" does not exist; senha=abc')
    monkeypatch.setattr(financeiro, 'get_financeiro', falha)
    r = cliente.get('/api/financeiro/dados')
    assert r.status_code == 500
    assert r.get_json()['erro'] == seguranca.MSG_ERRO_INTERNO
    assert 't_segredo' not in r.get_data(as_text=True)
    assert 't_segredo' in capsys.readouterr().out  # o detalhe fica no log do servidor


def test_envio_grande_demais_responde_413(cliente, logar, admin_id, monkeypatch):
    monkeypatch.setitem(painel.app.config, 'MAX_CONTENT_LENGTH', 1024)
    logar(admin_id)
    r = cliente.post('/admin/banco/logo', data={'tipo': 'cabecalho', 'arquivo': (io.BytesIO(b'0' * 4096), 'a.png')},
                     headers={'X-CSRF-Token': 'tok'}, content_type='multipart/form-data')
    assert r.status_code == 413


# ---------------------------------------------------------------- login e senhas

def test_armadilha_de_robo_recusa_login_mesmo_com_senha_certa(cliente, admin_id):
    r = cliente.post('/login', data={'usuario': 'admin', 'senha': 'SenhaAdmin1', 'site': 'http://spam'})
    assert r.status_code == 200 and 'inválidos' in r.get_data(as_text=True)
    with cliente.session_transaction() as s:
        assert 'usuario_id' not in s
    assert cliente.post('/login', data={'usuario': 'admin', 'senha': 'SenhaAdmin1', 'site': ''}).status_code == 302


def test_senha_definida_pelo_admin_tem_8_caracteres(admin_id):
    perfil = usuarios.listar_perfis()[0]['id']
    assert '8 caracteres' in usuarios.salvar_usuario(None, 'eva', 'Eva', 'curta12', perfil, True)
    assert usuarios.salvar_usuario(None, 'eva', 'Eva', 'longa123', perfil, True) is None


# ---------------------------------------------------------------- planilhas

def _importar_financeiro(cliente, conteudo, nome):
    tipo = next(iter(financeiro.TIPOS))
    return cliente.post(f'/admin/financeiro/importar/{tipo}', data={'csrf': 'tok', 'planilha': (io.BytesIO(conteudo), nome)},
                        content_type='multipart/form-data')


def test_planilha_conferida_pelo_conteudo(cliente, logar, admin_id):
    import area_restrita
    logar(admin_id)
    _importar_financeiro(cliente, b'<html><script>alert(1)</script></html>', 'relatorio.xlsx')
    with cliente.session_transaction() as s:
        assert any('Escolha a planilha' in m for _, m in s.get('_flashes', []))

    class Arquivo:
        def __init__(self, nome, conteudo):
            self.filename, self.stream = nome, io.BytesIO(conteudo)
    assert area_restrita._planilha_ok(Arquivo('a.xlsx', b'PK\x03\x04resto'))
    assert area_restrita._planilha_ok(Arquivo('a.xls', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1resto'), financeiro.EXTENSOES)
    assert not area_restrita._planilha_ok(Arquivo('a.xls', b'PK\x03\x04'), financeiro.EXTENSOES)
    assert not area_restrita._planilha_ok(Arquivo('a.csv', b'PK\x03\x04'))
    assert not area_restrita._planilha_ok(None)
