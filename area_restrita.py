"""
area_restrita.py

Login e área restrita (/admin) do painel: usuários, perfis de acesso,
configuração do banco, telas de lançamento (custos, produção hospitalar e
SIMAQ) e cadastros. painel.py chama registrar(app) uma vez.

Usuários e perfis ficam em usuarios.py (SQLite local). Na primeira execução
é criado o administrador com ADMIN_USER / ADMIN_PASS (sem ADMIN_PASS, a senha
é gerada e mostrada no console; a troca é obrigatória no primeiro acesso).
- Perfil administrador: tudo, inclusive /admin (usuários, perfis, banco).
- Demais perfis: só as páginas do sistema marcadas no perfil.
"""

import os
import secrets
import threading
import time
from datetime import datetime
from decimal import Decimal

import psycopg2
from flask import abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for

import banco_painel
import classificacao_exames
import config_banco
import financeiro
import importacao_custos
import lancamento_custos
import logos
import paginas_visiveis
import usuarios

# Rotas e ganchos ficam registrados aqui e entram no app em registrar(), com
# os mesmos nomes de endpoint (url_for('admin_menu') etc.).
_rotas = []
_ganchos = []


def _rota(regra, **opcoes):
    def decorar(funcao):
        _rotas.append((regra, funcao, opcoes))
        return funcao
    return decorar


def _gancho(tipo, *args):
    def decorar(funcao):
        _ganchos.append((tipo, funcao, args))
        return funcao
    return decorar


def _area_restrita(caminho):
    """Só /admin exige login; os painéis (e as APIs deles) são públicos."""
    return caminho == '/admin' or caminho.startswith('/admin/')


def _eh_requisicao_de_dados():
    return request.method != 'GET' or '/api/' in request.path or \
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _banco_pendente():
    """Servidor novo: a conexão com o banco ainda não foi configurada."""
    return not config_banco.configurado()


def _banco_indisponivel(mensagem, status=503):
    if _eh_requisicao_de_dados():
        return jsonify({'ok': False, 'erro': mensagem}), status
    return render_template('error.html', message=mensagem), status


@_gancho('errorhandler', config_banco.ConfiguracaoAusente)
def banco_nao_configurado(e):
    """Servidor novo: painéis abertos antes da configuração do banco."""
    return _banco_indisponivel(str(e))


@_gancho('errorhandler', psycopg2.OperationalError)
def banco_fora_do_ar(e):
    print(f'[painel] banco de dados inacessível em {request.path}: {str(e).strip()[:300]}', flush=True)
    return _banco_indisponivel('Não foi possível conectar ao banco de dados. Tente de novo em alguns minutos.')


def _sem_permissao():
    if _eh_requisicao_de_dados():
        return jsonify({'ok': False, 'erro': 'Seu perfil não tem acesso a esta página.'}), 403
    return render_template('error.html', message='Seu perfil não tem acesso a esta página. '
                                                 'Fale com o administrador do painel.'), 403


# Logout automático: a sessão termina após INATIVIDADE_MAX segundos sem
# nenhuma requisição de quem está logado. A página (static/js/sessao.js)
# avisa o uso em /sessao/ativa e sai sozinha quando o tempo acaba.
INATIVIDADE_MAX = 15 * 60
MSG_SESSAO_EXPIRADA = 'Sua sessão foi encerrada após 15 minutos sem uso. Entre novamente.'


@_gancho('before_request')
def controlar_acesso():
    """Carrega o usuário logado e verifica a permissão da rota pedida."""
    if request.path.startswith('/static/'):
        g.usuario = None
        return None  # arquivos estáticos: públicos, sem consultar o banco de usuários
    g.usuario =usuarios.obter_usuario(session['usuario_id']) if session.get('usuario_id') else None
    if g.usuario is not None and not g.usuario['ativo']:
        session.clear()
        g.usuario = None
    if g.usuario is not None:
        agora = time.time()
        ultimo = session.get('ultimo_acesso')
        if ultimo and agora - ultimo > INATIVIDADE_MAX:
            session.clear()
            session['sessao_expirada'] = True
            g.usuario = None
        else:
            session['ultimo_acesso'] = agora

    caminho = request.path
    # painel oculto em /admin/banco: só o administrador ainda abre pelo endereço
    if paginas_visiveis.oculta(caminho) and not (g.usuario and usuarios.eh_admin(g.usuario)):
        msg = 'Esta página está desativada. Fale com o administrador do painel.'
        if _eh_requisicao_de_dados():
            return jsonify({'ok': False, 'erro': msg}), 404
        return render_template('error.html', message=msg), 404
    if not _area_restrita(caminho):
        return None  # painéis, login e arquivos estáticos: públicos
    if g.usuario is None:
        if _eh_requisicao_de_dados():
            return jsonify({'ok': False, 'erro': 'Sessão expirada. Faça login novamente.'}), 401
        return redirect(url_for('login', proximo=request.full_path.rstrip('?')))

    # proteção contra envio forjado na área restrita: todo POST traz o token,
    # no formulário (campo csrf) ou no header X-CSRF-Token (chamadas fetch)
    if request.method == 'POST':
        enviado = request.form.get('csrf') or request.headers.get('X-CSRF-Token') or ''
        if not session.get('csrf') or not secrets.compare_digest(enviado, session['csrf']):
            if _eh_requisicao_de_dados() and not request.form:
                return jsonify({'ok': False, 'erro': 'Página expirada. Recarregue e tente de novo.'}), 400
            return render_template('error.html', message='Formulário expirado. Recarregue a página e tente de novo.'), 400

    # senha inicial ou redefinida: obriga a trocar antes de usar a área restrita
    if g.usuario.get('trocar_senha') and caminho not in ('/admin/senha',):
        if _eh_requisicao_de_dados():
            return jsonify({'ok': False, 'erro': 'Troque sua senha antes de continuar.'}), 403
        return redirect(url_for('admin_senha'))

    # servidor novo: depois da senha, o administrador configura o banco antes do resto
    if usuarios.eh_admin(g.usuario) and caminho != '/admin/senha' and not caminho.startswith('/admin/banco') \
            and _banco_pendente():
        if _eh_requisicao_de_dados():
            return jsonify({'ok': False, 'erro': 'Configure o banco de dados antes de continuar.'}), 409
        return redirect(url_for('admin_banco'))

    if caminho in ('/admin', '/admin/senha'):
        return None  # menu da área restrita (mostra só o que o perfil libera) e troca da própria senha
    if caminho.startswith('/admin/banco'):
        return None if usuarios.eh_admin(g.usuario) else _sem_permissao()  # só o administrador
    chave = usuarios.pagina_da_rota(caminho)
    if chave is None:
        return None if usuarios.eh_admin(g.usuario) else _sem_permissao()
    return None if usuarios.pode_acessar(g.usuario, chave) else _sem_permissao()


@_gancho('after_request')
def marcar_sessao(resposta):
    """Cookie painel_sessao=1 enquanto há alguém logado: o script de
    inatividade (static/js/sessao.js) só conta o tempo nesse caso."""
    if request.path.startswith('/static/'):
        return resposta
    if getattr(g, 'usuario', None):
        resposta.set_cookie('painel_sessao', '1', samesite='Lax')
    elif request.cookies.get('painel_sessao'):
        resposta.delete_cookie('painel_sessao')
    return resposta


@_rota('/sessao/ativa')
def sessao_ativa():
    """A página avisa que há uso (cliques, digitação): controlar_acesso já
    renovou o último acesso; não há conteúdo."""
    return '', 204


def _enderecos_logos():
    return logos.enderecos(lambda nome: url_for('static', filename=nome),
                           lambda tipo, v: url_for('logo_imagem', tipo=tipo, v=v))


@_gancho('template_global', 'logo')
def logo(tipo):
    """Endereço da logo nos templates (inclusive nas macros do _layout.html):
    logo('cabecalho') ou logo('carregando')."""
    return _enderecos_logos()[tipo]


@_rota('/logo/<tipo>')
def logo_imagem(tipo):
    """Logo enviada em Configuração do Banco (pública, como os painéis)."""
    if tipo not in logos.LOGOS:
        abort(404)
    caminho = logos.arquivo_personalizado(tipo)
    if caminho is None:
        return redirect(url_for('static', filename=logos.LOGOS[tipo][1]))
    extensao = caminho.rsplit('.', 1)[1]
    # o endereço muda (?v=) a cada troca: pode ficar em cache
    return send_file(caminho, mimetype=logos.MIMETYPES[extensao], max_age=7 * 24 * 3600)


@_gancho('context_processor')
def contexto_usuario():
    u = getattr(g, 'usuario', None)
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(16)
    return {
        'usuario_atual': u,
        'eh_admin': usuarios.eh_admin(u),
        'pode': lambda chave: usuarios.pode_acessar(u, chave),
        'csrf_token': session['csrf'],
    }


def _destino_seguro(proximo):
    # só aceita caminhos internos, para o login não redirecionar para outro site
    if proximo and proximo.startswith('/') and not proximo.startswith('//') and '\\' not in proximo:
        return proximo
    return url_for('admin_menu')  # o login serve à área restrita


# Tentativas de login erradas (em memória, por processo): depois de
# _LOGIN_MAX_POR_USUARIO erros no mesmo login e IP, ou _LOGIN_MAX_POR_IP no
# mesmo IP, o login fica bloqueado até a janela passar.
_LOGIN_JANELA = 15 * 60
_LOGIN_MAX_POR_USUARIO = 5
_LOGIN_MAX_POR_IP = 20
_login_falhas = {}
_login_lock = threading.Lock()


def _login_chaves(login_digitado):
    ip = request.remote_addr or '?'
    return ('ip', ip), ('usuario', ip, (login_digitado or '').strip().lower())


def _login_bloqueado(login_digitado):
    agora = time.monotonic()
    ip, usuario = _login_chaves(login_digitado)
    with _login_lock:
        for chave in (ip, usuario):
            _login_falhas[chave] = [t for t in _login_falhas.get(chave, []) if agora - t < _LOGIN_JANELA]
        return len(_login_falhas[usuario]) >= _LOGIN_MAX_POR_USUARIO or len(_login_falhas[ip]) >= _LOGIN_MAX_POR_IP


def _login_registrar(login_digitado, ok):
    ip, usuario = _login_chaves(login_digitado)
    with _login_lock:
        if ok:
            _login_falhas.pop(usuario, None)
            return
        for chave in (ip, usuario):
            _login_falhas.setdefault(chave, []).append(time.monotonic())


@_rota('/login', methods=['GET', 'POST'])
def login():
    proximo = request.values.get('proximo')
    erro = None
    if request.method == 'POST':
        login_digitado = request.form.get('usuario')
        if _login_bloqueado(login_digitado):
            erro = 'Muitas tentativas erradas. Aguarde alguns minutos e tente de novo.'
            return render_template('login.html', erro=erro, proximo=proximo or ''), 429
        u = usuarios.autenticar(login_digitado, request.form.get('senha'))
        if u is None and not request.form.get('senha') and _banco_pendente():
            # sem banco configurado: administrador entra sem senha para configurá-lo
            u = usuarios.liberar_admin_sem_senha(login_digitado)
        _login_registrar(login_digitado, bool(u))
        if u:
            session.clear()
            session['usuario_id'] = u['id']
            session['csrf'] = secrets.token_hex(16)
            session['ultimo_acesso'] = time.time()
            return redirect(_destino_seguro(proximo))
        erro = 'Usuário ou senha inválidos, ou usuário inativo.'
    elif session.pop('sessao_expirada', False):
        erro = MSG_SESSAO_EXPIRADA
    return render_template('login.html', erro=erro, proximo=proximo or '', banco_pendente=_banco_pendente(),
                           login_admin=os.environ.get('ADMIN_USER', 'admin'))


@_rota('/logout')
def logout():
    """inatividade=1: saída automática do script de inatividade; proximo: a
    página em que a pessoa estava (área restrita volta pelo login)."""
    session.clear()
    g.usuario = None
    if request.args.get('inatividade') != '1':
        return redirect(url_for('index'))
    session['sessao_expirada'] = True
    proximo = request.args.get('proximo') or ''
    seguro = proximo.startswith('/') and not proximo.startswith('//') and '\\' not in proximo
    if seguro and not _area_restrita(proximo.split('?')[0]):
        return redirect(proximo)  # painel público: continua na página, sem login
    return redirect(url_for('login', proximo=proximo if seguro else None))


@_rota('/admin')
def admin_menu():
    return render_template('admin.html')


@_rota('/admin/senha', methods=['GET', 'POST'])
def admin_senha():
    """Troca da própria senha (obrigatória com senha inicial ou redefinida)."""
    if request.method == 'POST':
        erro = usuarios.trocar_senha(g.usuario['id'], request.form.get('atual'),
                                     request.form.get('nova'), request.form.get('confirmacao'))
        if not erro:
            if usuarios.eh_admin(g.usuario) and _banco_pendente():
                return redirect(url_for('admin_banco'))  # primeiro acesso: passo 2, configurar o banco
            flash('Senha alterada.', 'ok')
            return redirect(url_for('admin_menu'))
        flash(erro, 'erro')
    return render_template('admin_senha.html', obrigatoria=g.usuario['trocar_senha'],
                           sem_senha=usuarios.sem_senha(g.usuario['id']),
                           configuracao_inicial=usuarios.eh_admin(g.usuario) and _banco_pendente())


# Quem tem acesso a Usuários/Perfis mas não é administrador só mexe em perfis
# e usuários cujo acesso caiba no seu próprio, e nunca no próprio perfil ou
# usuário (evita ganho de privilégio).
def _paginas_do_perfil(perfil):
    if not perfil:
        return set()
    return set(usuarios.CHAVES_PAGINAS) if perfil['admin'] else set(perfil['paginas'])


def _bloqueio_usuario(usuario_id, perfil_id=None):
    if usuarios.eh_admin(g.usuario):
        return None
    minhas = _paginas_do_perfil(g.usuario['perfil'])
    if usuario_id == g.usuario['id']:
        return 'Você não pode alterar o próprio usuário aqui; para a senha use "Trocar senha".'
    perfil = usuarios.obter_perfil(perfil_id) if perfil_id else None
    if perfil and (perfil['admin'] or not _paginas_do_perfil(perfil) <= minhas):
        return 'Você só pode dar perfis com acesso igual ou menor que o seu.'
    alvo = usuarios.obter_usuario(usuario_id) if usuario_id else None
    if alvo and alvo['perfil'] and (alvo['perfil']['admin'] or not _paginas_do_perfil(alvo['perfil']) <= minhas):
        return 'Você não pode alterar ou excluir um usuário com mais acesso que o seu.'
    return None


def _bloqueio_perfil(perfil_id, quer_admin=False, paginas=()):
    if usuarios.eh_admin(g.usuario):
        return None
    if quer_admin:
        return 'Só o administrador pode criar ou marcar um perfil como administrador.'
    if perfil_id is not None and perfil_id == g.usuario['perfil_id']:
        return 'Você não pode alterar ou excluir o próprio perfil.'
    minhas = _paginas_do_perfil(g.usuario['perfil'])
    if not set(paginas) <= minhas:
        return 'Você só pode liberar páginas a que você mesmo tem acesso.'
    perfil = usuarios.obter_perfil(perfil_id) if perfil_id else None
    if perfil and (perfil['admin'] or not _paginas_do_perfil(perfil) <= minhas):
        return 'Você não pode alterar ou excluir um perfil com mais acesso que o seu.'
    return None


# ---- usuários ----

@_rota('/admin/usuarios')
def admin_usuarios():
    editar_id = request.args.get('editar', type=int)
    return render_template(
        'admin_usuarios.html',
        usuarios=usuarios.listar_usuarios(),
        perfis=usuarios.listar_perfis(),
        editando=usuarios.obter_usuario(editar_id) if editar_id else None,
    )


@_rota('/admin/usuarios/salvar', methods=['POST'])
def admin_usuarios_salvar():
    usuario_id = request.form.get('id', type=int)
    erro = _bloqueio_usuario(usuario_id, request.form.get('perfil_id', type=int)) or usuarios.salvar_usuario(
        usuario_id,
        request.form.get('login'), request.form.get('nome'), request.form.get('senha'),
        request.form.get('perfil_id', type=int), request.form.get('ativo') == '1',
        trocar_senha=usuario_id != g.usuario['id'],  # senha dada por outra pessoa é provisória
    )
    if erro:
        flash(erro, 'erro')
        return redirect(url_for('admin_usuarios', editar=usuario_id) if usuario_id else url_for('admin_usuarios'))
    flash('Usuário salvo.', 'ok')
    return redirect(url_for('admin_usuarios'))


@_rota('/admin/usuarios/<int:usuario_id>/excluir', methods=['POST'])
def admin_usuarios_excluir(usuario_id):
    if usuario_id == g.usuario['id']:
        flash('Você não pode excluir o próprio usuário.', 'erro')
    else:
        erro = _bloqueio_usuario(usuario_id) or usuarios.excluir_usuario(usuario_id)
        flash(erro or 'Usuário excluído.', 'erro' if erro else 'ok')
    return redirect(url_for('admin_usuarios'))


# ---- perfis de acesso ----

@_rota('/admin/perfis')
def admin_perfis():
    editar_id = request.args.get('editar', type=int)
    return render_template(
        'admin_perfis.html',
        perfis=usuarios.listar_perfis(),
        paginas=usuarios.PAGINAS,
        nomes_paginas={chave: nome for chave, nome, _ in usuarios.PAGINAS},
        editando=usuarios.obter_perfil(editar_id) if editar_id else None,
    )


@_rota('/admin/perfis/salvar', methods=['POST'])
def admin_perfis_salvar():
    perfil_id = request.form.get('id', type=int)
    quer_admin = request.form.get('admin') == '1'
    erro = _bloqueio_perfil(perfil_id, quer_admin, request.form.getlist('paginas')) or usuarios.salvar_perfil(
        perfil_id, request.form.get('nome'), quer_admin, request.form.getlist('paginas'),
    )
    if erro:
        flash(erro, 'erro')
        return redirect(url_for('admin_perfis', editar=perfil_id) if perfil_id else url_for('admin_perfis'))
    flash('Perfil salvo.', 'ok')
    return redirect(url_for('admin_perfis'))


@_rota('/admin/perfis/<int:perfil_id>/excluir', methods=['POST'])
def admin_perfis_excluir(perfil_id):
    erro = _bloqueio_perfil(perfil_id) or usuarios.excluir_perfil(perfil_id)
    flash(erro or 'Perfil excluído.', 'erro' if erro else 'ok')
    return redirect(url_for('admin_perfis'))


def _config_do_formulario():
    """Configuração enviada pela página; senha em branco = manter a atual."""
    dados = request.get_json(silent=True) or {}
    atual = config_banco.carregar()
    cfg = {campo: str(dados.get(campo) or '').strip() for campo in config_banco.CAMPOS}
    if not cfg['password']:
        cfg['password'] = atual['password']
    return cfg


@_rota('/admin/banco')
def admin_banco():
    cfg = config_banco.carregar()
    tem_senha = bool(cfg.pop('password'))  # a senha nunca volta para o navegador
    return render_template('config_banco.html', cfg=cfg, tem_senha=tem_senha,
                           erro_config=config_banco.erro_arquivo(), paginas=paginas_visiveis.listar(),
                           logos=logos.listar(_enderecos_logos()), modulos=config_banco.MODULOS_HOSPITALAR,
                           configuracao_inicial=_banco_pendente())


@_rota('/admin/banco/logo', methods=['POST'])
def admin_banco_logo():
    """Troca uma logo: formulário com tipo e arquivo."""
    tipo = request.form.get('tipo', '')
    arquivo = request.files.get('arquivo')
    try:
        logos.salvar(tipo, arquivo.read(logos.TAMANHO_MAXIMO + 1) if arquivo else b'')
    except logos.LogoInvalida as e:
        return jsonify({'ok': False, 'erro': str(e)}), 400
    except OSError as e:
        return jsonify({'ok': False, 'erro': f'Não foi possível salvar: {e}'}), 500
    return jsonify({'ok': True, 'url': _enderecos_logos()[tipo]})


@_rota('/admin/banco/logo/restaurar', methods=['POST'])
def admin_banco_logo_restaurar():
    tipo = (request.get_json(silent=True) or {}).get('tipo', '')
    if tipo not in logos.LOGOS:
        return jsonify({'ok': False, 'erro': 'Logo desconhecida.'}), 400
    try:
        logos.restaurar(tipo)
    except OSError as e:
        return jsonify({'ok': False, 'erro': f'Não foi possível restaurar: {e}'}), 500
    return jsonify({'ok': True, 'url': _enderecos_logos()[tipo]})


@_rota('/admin/banco/paginas', methods=['POST'])
def admin_banco_paginas():
    """Páginas visíveis no menu inicial: recebe {"visiveis": [chaves]}."""
    visiveis = set((request.get_json(silent=True) or {}).get('visiveis') or [])
    try:
        paginas_visiveis.salvar([c for c in paginas_visiveis.CHAVES if c not in visiveis])
    except OSError as e:
        return jsonify({'ok': False, 'erro': f'Não foi possível salvar: {e}'}), 500
    return jsonify({'ok': True, 'paginas': paginas_visiveis.listar()})


@_rota('/admin/banco/testar', methods=['POST'])
def admin_banco_testar():
    cfg = _config_do_formulario()
    erros = config_banco.validar(cfg)
    if erros:
        return jsonify({'ok': False, 'erro': ' '.join(erros)}), 400
    return jsonify(config_banco.testar(cfg))


@_rota('/admin/banco/salvar', methods=['POST'])
def admin_banco_salvar():
    cfg = _config_do_formulario()
    erros = config_banco.validar(cfg)
    if erros:
        return jsonify({'ok': False, 'erro': ' '.join(erros)}), 400
    try:
        config_banco.salvar(cfg)
    except (OSError, ValueError) as e:  # ValueError: chave de criptografia malformada
        return jsonify({'ok': False, 'erro': f'Não foi possível salvar: {e}'}), 500
    # banco ou schema dos dados podem ter mudado: refaz tabelas/migração no destino novo
    pronto = banco_painel.reiniciar()
    tabelas = []
    if pronto:
        try:
            tabelas = banco_painel.resumo_tabelas()
        except Exception:  # só informativo: a configuração já foi salva
            tabelas = []
    return jsonify({'ok': True, 'schema_painel_pronto': pronto, 'erro_schema_painel': banco_painel.erro(),
                    'tabelas_painel': tabelas})


# ---- lançamento de dados (área restrita): produção hospitalar e SIMAQ ----
# Uma tela por tabela de banco_painel, todas com a mesma estrutura: grade do
# mês com os itens já conhecidos, linha para item novo e importação de xlsx.
# Custos têm tela própria, por unidade (mais abaixo).

LANCAMENTOS = {
    'producao': {'tabela': banco_painel.PRODUCAO, 'titulo': 'Lançar Dados de Produção',
                 'grupo': 'servico', 'pagina': 'producao_hospitalar', 'nome_pagina': 'Produção Hospitalar',
                 'cadastro': 'procedimento'},
    'simaq': {'tabela': banco_painel.SIMAQ, 'titulo': 'Lançar Dados do SIMAQ',
              'grupo': 'tipo', 'pagina': 'indicadores', 'nome_pagina': 'Indicadores SIMAQ',
              'cadastro': 'indicador'},
}


@_gancho('template_filter', 'num_br')
def num_br(valor, formato='decimal'):
    """Número no formato brasileiro para os campos de lançamento.

    Mostra todas as casas do valor gravado (moeda: no mínimo 2), para que
    reenviar o formulário sem mexer na linha não arredonde o que está no banco."""
    if valor is None or valor == '':
        return ''
    if formato == 'texto':
        return valor
    d = Decimal(str(valor)).normalize()
    casas = max({'moeda': 2}.get(formato, 0), -min(d.as_tuple().exponent, 0))
    return f'{d:,.{casas}f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


@_gancho('template_filter', 'moeda_br')
def moeda_br(valor):
    """Valor para exibir (não para campo de formulário): 2 casas, formato brasileiro."""
    if valor is None or valor == '':
        return ''
    return f'{float(valor):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def _mes_anterior():
    """Mês anterior ao atual (AAAA-MM): o padrão quando ainda não há lançamento."""
    hoje = datetime.now()
    return f'{hoje.year - 1}-12' if hoje.month == 1 else f'{hoje.year}-{hoje.month - 1:02d}'


def _mes_valido(mes):
    try:
        datetime.strptime(mes or '', '%Y-%m')
        return True
    except ValueError:
        return False


def _ler_campo(coluna, texto):
    if coluna.formato == 'texto':
        return (texto or '').strip() or None
    return banco_painel.ler_numero(texto, coluna.formato)


def _rotulo_item(t, grupo, chaves):
    return ' · '.join(chaves[c] for c in t.campos_chave if c != grupo) or chaves[t.campos_chave[0]]


def admin_lancamento(chave):
    cfg = LANCAMENTOS[chave]
    t = cfg['tabela']
    if not banco_painel.pronto():
        return render_template('admin_lancamento.html', chave=chave, cfg=cfg, t=t,
                               indisponivel=banco_painel.erro() or 'Banco de dados dos lançamentos indisponível.', sql=banco_painel.sql_criar_schema())
    meses = t.meses()
    mes = request.args.get('mes')
    if not _mes_valido(mes):
        mes = meses[0]['mes'] if meses else _mes_anterior()  # último mês lançado
    do_mes = t.do_mes(mes)
    grupo = cfg['grupo']
    linhas = []
    for ultimo in t.itens():
        k = t.chave_de(ultimo)
        atual = do_mes.get(k)
        if not ultimo['ativo'] and atual is None:
            continue  # inativo (tela Cadastros): só aparece nos meses em que já tem lançamento
        chaves = {c: ultimo[c] for c in t.campos_chave}
        valores = {c.nome: (atual[c.nome] if atual else (ultimo[c.nome] if c.herda else None)) for c in t.colunas}
        linhas.append({
            'chaves': chaves,
            'valores': valores,
            'grupo': (atual or ultimo).get(grupo) or 'Sem classificação',
            'rotulo': _rotulo_item(t, grupo, chaves),
            'inativo': not ultimo['ativo'],
            'atualizado_em': atual['atualizado_em'] if atual else None,
            'atualizado_por': atual['atualizado_por'] if atual else None,
        })
    linhas.sort(key=lambda l: (l['grupo'], l['rotulo']))
    return render_template('admin_lancamento.html', chave=chave, cfg=cfg, t=t, mes=mes, linhas=linhas,
                           meses=meses, coluna_total=next((c for c in t.colunas if c.total), None))


def admin_lancamento_salvar(chave):
    cfg = LANCAMENTOS[chave]
    t = cfg['tabela']
    mes = request.form.get('mes') or ''
    if not _mes_valido(mes):
        flash('Informe o mês do lançamento.', 'erro')
        return redirect(url_for(f'admin_{chave}'))
    if not banco_painel.pronto():
        flash(banco_painel.erro() or 'Banco indisponível.', 'erro')
        return redirect(url_for(f'admin_{chave}', mes=mes))

    gravar, remover, erros = [], [], []
    numeros = sorted({k.rsplit('__', 1)[1] for k in request.form if k.startswith('k__')})
    for n in numeros:
        chaves = {c: (request.form.get(f'k__{c}__{n}') or '').strip() for c in t.campos_chave}
        try:
            valores = {c.nome: _ler_campo(c, request.form.get(f'v__{c.nome}__{n}')) for c in t.colunas if c.editavel}
        except ValueError:
            erros.append(_rotulo_item(t, cfg['grupo'], chaves))
            continue
        if not all(chaves.values()):
            continue
        if t.em_branco(valores):
            remover.append(tuple(chaves[c] for c in t.campos_chave))  # linha em branco = sem lançamento no mês
        else:
            gravar.append({'mes': mes, **chaves, **valores})

    if erros:
        flash('Valor inválido em: ' + '; '.join(erros) + '. Nada foi salvo.', 'erro')
        return redirect(url_for(f'admin_{chave}', mes=mes))
    try:
        t.salvar(gravar, g.usuario['login'])  # só itens das tabelas de cadastro
    except banco_painel.ItensNaoCadastrados as e:
        flash(f'{e} Nada foi salvo.', 'erro')
        return redirect(url_for(f'admin_{chave}', mes=mes))
    existentes = t.do_mes(mes)
    removidos = [k for k in remover if k in existentes]
    t.excluir(mes, removidos)
    flash(f'Dados de {mes[5:]}/{mes[:4]} salvos: {len(gravar)} itens'
          + (f', {len(removidos)} removidos.' if removidos else '.'), 'ok')
    return redirect(url_for(f'admin_{chave}', mes=mes))


def admin_lancamento_importar(chave):
    t = LANCAMENTOS[chave]['tabela']
    arquivo = request.files.get('planilha')
    if not arquivo or not arquivo.filename.lower().endswith('.xlsx'):
        flash('Escolha uma planilha .xlsx.', 'erro')
        return redirect(url_for(f'admin_{chave}'))
    if not banco_painel.pronto():
        flash(banco_painel.erro() or 'Banco indisponível.', 'erro')
        return redirect(url_for(f'admin_{chave}'))
    import tempfile
    with tempfile.TemporaryDirectory() as pasta:
        caminho = os.path.join(pasta, 'planilha.xlsx')
        arquivo.save(caminho)
        try:
            qtd, novos = t.importar_cadastrando(caminho, g.usuario['login'])
        except Exception as e:
            flash(f'Não foi possível importar a planilha: {e}', 'erro')
            return redirect(url_for(f'admin_{chave}'))
    flash(f'Planilha importada: {qtd} lançamentos gravados.' + _texto_itens_novos(novos), 'ok')
    return redirect(url_for(f'admin_{chave}'))


def _texto_itens_novos(novos):
    """Aviso dos itens que a importação cadastrou (para conferir em Cadastros)."""
    if not novos:
        return ''
    amostra = '; '.join(' / '.join(k) for k in novos[:5])
    mais = f' e mais {len(novos) - 5}' if len(novos) > 5 else ''
    return f' {len(novos)} item(ns) novo(s) cadastrado(s): {amostra}{mais}. Confira na tela de Cadastros.'


# ---- lançar dados do financeiro (financeiro.py): relatórios do SIAFE ----

@_rota('/admin/financeiro')
def admin_financeiro():
    if not banco_painel.pronto():
        return render_template('admin_financeiro.html', indisponivel=banco_painel.erro()
                               or 'Banco de dados dos lançamentos indisponível.', sql=banco_painel.sql_criar_schema())
    return render_template('admin_financeiro.html', tipos=financeiro.TIPOS, importacoes=financeiro.importacoes())


@_rota('/admin/financeiro/importar/<tipo>', methods=['POST'])
def admin_financeiro_importar(tipo):
    if tipo not in financeiro.TIPOS:
        abort(404)
    arquivo = request.files.get('planilha')
    nome = arquivo.filename if arquivo else ''
    if not nome or not nome.lower().endswith(financeiro.EXTENSOES):
        flash('Escolha a planilha exportada do SIAFE (.xls ou .xlsx).', 'erro')
        return redirect(url_for('admin_financeiro'))
    if not banco_painel.pronto():
        flash(banco_painel.erro() or 'Banco indisponível.', 'erro')
        return redirect(url_for('admin_financeiro'))
    import tempfile
    with tempfile.TemporaryDirectory() as pasta:
        caminho = os.path.join(pasta, 'planilha' + os.path.splitext(nome)[1].lower())
        arquivo.save(caminho)
        try:
            lido = financeiro.importar(tipo, caminho, nome, g.usuario['login'])
        except financeiro.PlanilhaInvalida as e:
            flash(f'Planilha não importada: {e}', 'erro')
            return redirect(url_for('admin_financeiro'))
        except Exception as e:
            flash(f'Não foi possível importar a planilha: {e}', 'erro')
            return redirect(url_for('admin_financeiro'))
    totais = lido['totais']
    diferentes = [k for k, ok in totais['confere'].items() if not ok]
    texto = (f'{financeiro.TIPOS[tipo]} importados ({lido["referencia"] or "sem período"}): '
             f'{len(lido["linhas"])} linhas. ')
    if not diferentes:
        texto += 'A soma das linhas confere com o total do relatório.'
    elif diferentes == ['valor_liquidado_a_pagar']:
        # esperado: o relatório só detalha os empenhos com valor a liquidar
        texto += ('O valor a liquidar confere com o total do relatório; o liquidado a pagar vem do total do '
                  'relatório, que inclui empenhos não detalhados (veja abaixo).')
    else:
        texto += ('Atenção: a soma das linhas difere do total do relatório em '
                  + ', '.join(k.replace('valor_', '').replace('_', ' ') for k in diferentes) + ' (veja abaixo).')
    flash(texto, 'ok')
    return redirect(url_for('admin_financeiro'))


# ---- lançamento de custos por unidade (lancamento_custos.py) ----
# O valor de cada despesa é lançado por unidade (centro de custo); o total do
# hospital por despesa (página Custos) é a soma das unidades.

def _texto_mes(mes):
    return f'{mes[5:]}/{mes[:4]}'


@_rota('/admin/custos')
def admin_custos():
    if not banco_painel.pronto():
        return render_template('admin_lancamento_custos.html',
                               indisponivel=banco_painel.erro() or 'Banco de dados dos lançamentos indisponível.',
                               sql=banco_painel.sql_criar_schema())
    mes = request.args.get('mes')
    if not _mes_valido(mes):
        mes = lancamento_custos.ultimo_mes() or _mes_anterior()  # último mês lançado
    unidades = lancamento_custos.unidades(mes)
    unidade_id = request.args.get('unidade', type=int)
    unidade = next((u for u in unidades if u['id'] == unidade_id), unidades[0] if unidades else None)
    indice = unidades.index(unidade) if unidade else 0
    itens = lancamento_custos.itens(mes, unidade['id']) if unidade else []
    tipos = lancamento_custos.agrupar(itens, 'valor')  # categoria > subcategoria > itens
    despesas = lancamento_custos.despesas_do_mes(mes)
    return render_template(
        'admin_lancamento_custos.html', mes=mes, unidades=unidades, unidade=unidade, tipos=tipos,
        grupos_despesas=lancamento_custos.agrupar(despesas, 'total'),
        anterior=unidades[indice - 1] if unidade and indice > 0 else None,
        proxima=unidades[indice + 1] if unidade and indice + 1 < len(unidades) else None,
        situacao=lancamento_custos.situacao(mes), despesas=despesas,
        total_mes=sum(d['total'] for d in despesas), meses=lancamento_custos.meses())


@_rota('/admin/custos/salvar', methods=['POST'])
def admin_custos_salvar():
    mes = request.form.get('mes') or ''
    unidade_id = request.form.get('unidade_id', type=int)
    if not _mes_valido(mes) or not unidade_id:
        flash('Informe o mês e a unidade.', 'erro')
        return redirect(url_for('admin_custos'))
    voltar = redirect(url_for('admin_custos', mes=mes, unidade=unidade_id))
    if not banco_painel.pronto():
        flash(banco_painel.erro() or 'Banco indisponível.', 'erro')
        return voltar
    valores, invalidos = {}, []
    for chave, texto in request.form.items():
        if chave.startswith('v__'):
            try:
                valores[int(chave[3:])] = banco_painel.ler_numero(texto, 'moeda')
            except ValueError:
                invalidos.append(request.form.get(f'n__{chave[3:]}') or chave[3:])
    if invalidos:
        flash('Valor inválido em: ' + '; '.join(invalidos) + '. Nada foi salvo.', 'erro')
        return voltar
    try:
        gravados, removidos = lancamento_custos.salvar(mes, unidade_id, valores, g.usuario['login'])
    except lancamento_custos.MesSoTotal as e:
        flash(str(e), 'erro')
        return voltar
    nome = request.form.get('unidade_nome') or 'unidade'
    flash(f'{nome} — {_texto_mes(mes)}: {gravados} despesas com valor'
          + (f', {removidos} removidas' if removidos else '') + '. Total por despesa do mês atualizado.', 'ok')
    seguir = request.args.get('seguir', type=int)  # "Salvar e ir para a próxima unidade"
    return redirect(url_for('admin_custos', mes=mes, unidade=seguir)) if seguir else voltar


# ---- importação da planilha do setor de custos (aba GERAL), com pré-visualização ----
# A tela de lançamento envia a planilha para uma janela nova (target=_blank);
# ela fica num lote de preparação até a confirmação, que grava o mês, fecha a
# janela e recarrega a tela de lançamento (importacao_custos.py).

def _fechar_janela(mes=None):
    """Página que volta para a tela de lançamento de custos (janela que abriu a
    pré-visualização) e fecha esta; sem a janela de origem, só redireciona."""
    return render_template('admin_importacao_fim.html',
                           destino=url_for('admin_custos', mes=mes) if mes else url_for('admin_custos'))


def _lote_do_usuario(lote):
    dados = importacao_custos.carregar(lote, request.values.get('mes') if _mes_valido(request.values.get('mes')) else None)
    if dados is None:
        return None, 'Esta importação não existe mais: já foi confirmada, cancelada ou expirou.'
    if dados['usuario'] != g.usuario['login'] and not usuarios.eh_admin(g.usuario):
        return None, 'Esta importação foi enviada por outro usuário.'
    return dados, None


@_rota('/admin/custos/importar-geral', methods=['POST'])
def admin_custos_importar_geral():
    arquivo = request.files.get('planilha')
    mes = request.form.get('mes') or ''
    erro = None
    if not arquivo or not arquivo.filename.lower().endswith('.xlsx'):
        erro = 'Escolha a planilha do setor de custos (.xlsx).'
    elif not _mes_valido(mes):
        erro = 'Informe o mês de referência da planilha.'
    elif not banco_painel.pronto():
        erro = banco_painel.erro() or 'Banco indisponível.'
    if erro is None:
        import tempfile
        with tempfile.TemporaryDirectory() as pasta:
            caminho = os.path.join(pasta, 'planilha.xlsx')
            arquivo.save(caminho)
            try:
                lote = importacao_custos.preparar(caminho, arquivo.filename, mes, g.usuario['login'])
            except importacao_custos.ErroPlanilha as e:
                erro = f'Planilha fora do padrão: {e}'
            except Exception as e:
                erro = f'Não foi possível ler a planilha: {e}'
    if erro:
        return render_template('admin_importacao_custos.html', erro=erro), 400
    return redirect(url_for('admin_custos_importacao', lote=lote))


@_rota('/admin/custos/importacao/<lote>')
def admin_custos_importacao(lote):
    dados, erro = _lote_do_usuario(lote)
    if erro:
        return render_template('admin_importacao_custos.html', erro=erro), 404
    return render_template('admin_importacao_custos.html', d=dados)


@_rota('/admin/custos/importacao/<lote>/confirmar', methods=['POST'])
def admin_custos_importacao_confirmar(lote):
    dados, erro = _lote_do_usuario(lote)
    mes = request.form.get('mes') or ''
    if erro is None and not _mes_valido(mes):
        erro = 'Informe o mês de referência da planilha.'
    if erro:
        return render_template('admin_importacao_custos.html', erro=erro), 400
    try:
        r = importacao_custos.confirmar(lote, mes, g.usuario['login'])
    except Exception as e:
        flash(f'A importação não foi gravada (nada mudou no banco): {e}', 'erro')
        return redirect(url_for('admin_custos_importacao', lote=lote, mes=mes))
    flash(f'Planilha {dados["arquivo"]} importada em {mes[5:]}/{mes[:4]}: {r["itens"]} itens de custo, '
          f'{r["valores_unidade"]} valores por unidade, {r["rateio"]} valores de rateio e a produção de '
          f'{r["unidades_com_producao"]} unidades.', 'ok')
    return _fechar_janela(mes)


@_rota('/admin/custos/importacao/<lote>/cancelar', methods=['POST'])
def admin_custos_importacao_cancelar(lote):
    dados, erro = _lote_do_usuario(lote)
    if erro is None:
        importacao_custos.cancelar(lote)
        flash('Importação cancelada; nada foi gravado.', 'ok')
    return _fechar_janela()


# ---- cadastros (área restrita): tipos, itens, serviços, procedimentos e indicadores ----
# Os lançamentos só aceitam itens destas tabelas.

@_rota('/admin/cadastros')
def admin_cadastros():
    if not banco_painel.pronto():
        return render_template('admin_cadastros.html', indisponivel=banco_painel.erro() or 'Banco de dados dos lançamentos indisponível.',
                               sql=banco_painel.sql_criar_schema())
    registros = {c.tabela: c.listar() for c in banco_painel.CADASTROS}
    grupos = {}
    for c in banco_painel.CADASTROS:
        grupos.setdefault(c.grupo, []).append(c)
    # seção Classificação de Exames: os exames vêm do sistema hospitalar, que pode estar fora
    try:
        exames, erro_exames = classificacao_exames.exames(), None
    except psycopg2.Error as e:
        exames, erro_exames = [], f'Exames do sistema hospitalar indisponíveis: {e}'.strip()
    return render_template('admin_cadastros.html', grupos=grupos, registros=registros,
                           classificacoes=classificacao_exames.listar(), exames=exames, erro_exames=erro_exames)


@_rota('/admin/cadastros/<tabela>/salvar', methods=['POST'])
def admin_cadastros_salvar(tabela):
    cad = banco_painel.CADASTROS_POR_TABELA.get(tabela)
    if cad is None:
        return 'Cadastro não encontrado.', 404
    nome = (request.form.get('nome') or '').strip()
    extras = {}
    for c in cad.extras:
        try:
            extras[c.nome] = _ler_campo(c, request.form.get(c.nome))
        except ValueError:
            flash(f'{cad.titulo}: {c.rotulo} inválido.', 'erro')
            return redirect(url_for('admin_cadastros', _anchor=tabela))
    erro = cad.salvar(request.form.get('id', type=int), nome,
                      {tab: request.form.get(f'{tab}_id', type=int) for tab, _ in cad.pais}, extras,
                      {tab: request.form.get(f'{tab}_id', type=int) for tab, *_ in cad.opcionais})
    flash(f'{cad.titulo}: {erro}' if erro else f'{cad.titulo}: "{nome}" salvo.', 'erro' if erro else 'ok')
    return redirect(url_for('admin_cadastros', _anchor=tabela))


@_rota('/admin/cadastros/<tabela>/<int:registro_id>/ativo', methods=['POST'])
def admin_cadastros_ativo(tabela, registro_id):
    cad = banco_painel.CADASTROS_POR_TABELA.get(tabela)
    if cad is None:
        return 'Cadastro não encontrado.', 404
    ativo = request.form.get('ativo') == '1'
    nome = cad.definir_ativo(registro_id, ativo)
    if nome is None:
        flash(f'{cad.titulo}: registro não encontrado.', 'erro')
    else:
        flash(f'{cad.titulo}: "{nome}" {"ativado" if ativo else "inativado"}.', 'ok')
    return redirect(url_for('admin_cadastros', _anchor=tabela))


@_rota('/admin/cadastros/<tabela>/<int:registro_id>/excluir', methods=['POST'])
def admin_cadastros_excluir(tabela, registro_id):
    cad = banco_painel.CADASTROS_POR_TABELA.get(tabela)
    if cad is None:
        return 'Cadastro não encontrado.', 404
    if not usuarios.eh_admin(g.usuario):  # os demais perfis só inativam
        flash('Só o administrador pode excluir cadastros; use Inativar.', 'erro')
        return redirect(url_for('admin_cadastros', _anchor=tabela))
    erro = cad.excluir(registro_id)
    flash(f'{cad.titulo}: {erro}' if erro else f'{cad.titulo}: registro excluído.', 'erro' if erro else 'ok')
    return redirect(url_for('admin_cadastros', _anchor=tabela))


# ---------------------------------------------------------------- classificação dos exames

# Fica na tela Cadastros (seção Classificação de Exames); a permissão é a de Cadastros.

def _voltar_classificacoes():
    return redirect(url_for('admin_cadastros', _anchor='classificacao-exames'))


@_rota('/admin/classificacoes')
def admin_classificacoes():
    return _voltar_classificacoes()  # endereço antigo


@_rota('/admin/classificacoes/salvar', methods=['POST'])
def admin_classificacoes_salvar():
    nome = request.form.get('nome')
    erro = classificacao_exames.salvar(request.form.get('id', type=int), nome)
    flash(erro or f'Classificação "{" ".join((nome or "").split())}" salva.', 'erro' if erro else 'ok')
    return _voltar_classificacoes()


@_rota('/admin/classificacoes/<int:classificacao_id>/excluir', methods=['POST'])
def admin_classificacoes_excluir(classificacao_id):
    if not usuarios.eh_admin(g.usuario):
        flash('Só o administrador pode excluir classificações.', 'erro')
        return _voltar_classificacoes()
    erro = classificacao_exames.excluir(classificacao_id)
    flash(erro or 'Classificação excluída.', 'erro' if erro else 'ok')
    return _voltar_classificacoes()


@_rota('/admin/classificacoes/associar', methods=['POST'])
def admin_classificacoes_associar():
    """Recebe {"mudancas": {isn_exame: classificacao_id ou null}}."""
    mudancas = (request.get_json(silent=True) or {}).get('mudancas')
    if not isinstance(mudancas, dict):
        return jsonify({'ok': False, 'erro': 'Dados inválidos.'}), 400
    qtd, erro = classificacao_exames.associar(mudancas)
    if erro:
        return jsonify({'ok': False, 'erro': erro}), 400
    return jsonify({'ok': True, 'alterados': qtd})


def registrar(app):
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
    for tipo, funcao, args in _ganchos:
        registrar_gancho = getattr(app, tipo)  # app.before_request(f), app.template_filter(nome)(f)...
        (registrar_gancho(*args) if args else registrar_gancho)(funcao)
    for regra, funcao, opcoes in _rotas:
        app.add_url_rule(regra, view_func=funcao, **opcoes)
    for chave in LANCAMENTOS:
        app.add_url_rule(f'/admin/{chave}', f'admin_{chave}', admin_lancamento,
                         defaults={'chave': chave})
        app.add_url_rule(f'/admin/{chave}/salvar', f'admin_{chave}_salvar', admin_lancamento_salvar,
                         methods=['POST'], defaults={'chave': chave})
        app.add_url_rule(f'/admin/{chave}/importar', f'admin_{chave}_importar', admin_lancamento_importar,
                         methods=['POST'], defaults={'chave': chave})
    usuarios.iniciar()
    banco_painel.iniciar_em_segundo_plano()  # tabelas do schema painel e migração das planilhas (se o schema existir)
