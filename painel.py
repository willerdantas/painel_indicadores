import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, jsonify, request, session, redirect, url_for, flash, g
import secrets
import json
import pandas as pd
import os
import base64
from datetime import datetime, timedelta
from indicadores_hcasg import get_filtros as hcasg_get_filtros
from indicadores_hcasg import get_mensal_hospitalar, get_mensal_emergencia
from indicadores_hcasg import get_indicadores_hospitalar, get_indicadores_emergencia
import indicadores_hcasg as hcasg
import config_banco
import ambulatorio
import usuarios
import banco_painel
import custos

app = Flask(__name__)


def _carregar_secret_key():
    """Chave das sessões de login: FLASK_SECRET_KEY ou um arquivo gerado na
    primeira execução (assim o login sobrevive a reinícios do servidor)."""
    if os.environ.get('FLASK_SECRET_KEY'):
        return os.environ['FLASK_SECRET_KEY']
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.secret_key')
    try:
        with open(caminho, encoding='utf-8') as f:
            chave = f.read().strip()
        if chave:
            return chave
    except OSError:
        pass
    chave = secrets.token_hex(32)
    with open(caminho, 'w', encoding='utf-8') as f:
        f.write(chave)
    return chave


app.secret_key = _carregar_secret_key()

# Configura��es
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_DADOS = os.path.join(BASE_DIR, 'dados')
ARQUIVO_EXCEL = 'PRODUÇÃO HOSPITALAR CONSOLIDADA.xlsx'
ARQUIVO_LOGO = 'logo.png'

# Configuração do banco PostgreSQL: centralizada em config_banco.py
# (editável pela página /admin/banco, variáveis DB_* ou padrões).
def get_db_connection():
    """Cria e retorna uma conexão com o PostgreSQL"""
    return config_banco.conectar()


# Margem de segurança da faixa de IDs: cobre solicitações lançadas fora de
# ordem (com data até alguns dias diferente da posição do ID).
_MARGEM_FAIXA_EXAMES = timedelta(days=7)

# Origem da solicitação de exame, pela clínica solicitante (t_clinica):
#   externo    = 2 AMBULATÓRIO, 4 EXAME, 41 UNACON
#   emergência = 1 EMERGÊNCIA, 10 SALA DE MEDICAÇÃO, 13 SALA DE PARADA,
#                14 SALA DE PARADA - ESTABILIZAÇÃO, 43 SALA MEDICAÇÃO (CARDIO/PNEUMO),
#                45 SALA MEDICAÇÃO (COVID)
#   interno    = todas as demais
_EXAMES_ORIGEM_EXTERNO_SQL = "2, 4, 41"
_EXAMES_ORIGEM_EMERGENCIA_SQL = "1, 10, 13, 14, 43, 45"


def _faixa_ids_exame_solic(cur, data_inicial, data_final):
    """Faixa [id_ini, id_fim) de t_exame_solic que contém as solicitações do
    período. A tabela (~1,5 GB) não tem índice em dat_solicitacao, e filtrar
    só pela data obriga a ler a tabela inteira a cada consulta. Como
    isn_exame_solic é sequencial e acompanha a data de solicitação, a faixa é
    achada por busca binária no índice da chave primária (~25 consultas
    instantâneas); a consulta principal continua filtrando pela data, então a
    faixa só reduz o que é lido. Testado contra a varredura completa
    (contagens idênticas)."""
    cur.execute("SELECT MIN(isn_exame_solic) AS v FROM t_exame_solic")
    id_min = cur.fetchone()['v']
    cur.execute("SELECT MAX(isn_exame_solic) AS v FROM t_exame_solic")
    id_max = cur.fetchone()['v']
    if id_min is None:
        return 0, 0

    def primeiro_id_a_partir(dt):
        lo, hi = int(id_min), int(id_max) + 1
        while lo < hi:
            meio = (lo + hi) // 2
            cur.execute("""
                SELECT isn_exame_solic, dat_solicitacao FROM t_exame_solic
                WHERE isn_exame_solic >= %s AND dat_solicitacao IS NOT NULL
                ORDER BY isn_exame_solic LIMIT 1
            """, [meio])
            r = cur.fetchone()
            if r is None or r['dat_solicitacao'] >= dt:
                hi = meio
            else:
                lo = int(r['isn_exame_solic']) + 1
        return lo

    inicio = datetime.combine(data_inicial - _MARGEM_FAIXA_EXAMES, datetime.min.time())
    fim = datetime.combine(data_final + timedelta(days=1) + _MARGEM_FAIXA_EXAMES, datetime.min.time())
    return primeiro_id_a_partir(inicio), primeiro_id_a_partir(fim)

def carregar_dados():
    """Produção hospitalar: tabela painel.producao_hospitalar (a planilha
    original enquanto o schema painel não existir)."""
    try:
        linhas = banco_painel.PRODUCAO.dados()
    except Exception as e:
        print(f"[ERRO] Falha ao carregar a produção hospitalar: {e}")
        return pd.DataFrame()
    if not linhas:
        return pd.DataFrame()
    # Dia 5, como na planilha original: o JavaScript da página usa
    # new Date('AAAA-MM-DD'), que no fuso de Brasília levaria o dia 1 para o
    # mês anterior.
    df = pd.DataFrame({
        'Mês Referência': pd.to_datetime([f"{r['mes']}-05" for r in linhas]),
        'Serviço': [r['servico'] for r in linhas],
        'Tipo Procedimento': [r['tipo'] for r in linhas],
        'Exame/Procedimento': [r['procedimento'] for r in linhas],
        'Quantidade': [r['quantidade'] for r in linhas],
    }).sort_values('Mês Referência')
    df['Mês Formatado'] = df['Mês Referência'].dt.strftime('%d/%m/%Y')
    return df

def get_encoded_logo():
    """Converte o logo para base64 para incorpora��o no HTML"""
    caminho_logo = os.path.join(PASTA_DADOS, ARQUIVO_LOGO)
    if os.path.exists(caminho_logo):
        with open(caminho_logo, 'rb') as f:
            encoded = base64.b64encode(f.read()).decode('ascii')
        return f'data:image/png;base64,{encoded}'
    return ''

def calcular_kpis(df):
    """Calcula KPIs básicos para produção hospitalar"""
    if df.empty:
        return {
            'total_procedimentos': 0,
            'servico_mais_utilizado': '',
            'exame_mais_realizado': '',
            'media_mensal': 0,
            'crescimento': 0
        }
    # Use os nomes de colunas corretos e normalizados
    total_procedimentos = int(df['Quantidade'].sum())
    servico_col = 'Serviço' if 'Serviço' in df.columns else next((c for c in df.columns if 'Servi' in c), None)
    exame_col = 'Exame/Procedimento' if 'Exame/Procedimento' in df.columns else next((c for c in df.columns if 'Exame' in c), None)
    servico_mais_utilizado = df.groupby(servico_col)['Quantidade'].sum().idxmax() if servico_col and not df[servico_col].isnull().all() else ''
    exame_mais_realizado = df.groupby(exame_col)['Quantidade'].sum().idxmax() if exame_col and not df[exame_col].isnull().all() else ''
    media_mensal = int(df.groupby([df['Mês Referência'].dt.year, df['Mês Referência'].dt.month])['Quantidade'].sum().mean())
    crescimento = 0
    anos = df['Mês Referência'].dt.year.unique()
    if len(anos) >= 2:
        df_anual = df.groupby(df['Mês Referência'].dt.year)['Quantidade'].sum().reset_index()
        df_anual['Crescimento'] = df_anual['Quantidade'].pct_change() * 100
        crescimento = round(df_anual['Crescimento'].iloc[-1], 2)
    return {
        'total_procedimentos': total_procedimentos,
        'servico_mais_utilizado': servico_mais_utilizado,
        'exame_mais_realizado': exame_mais_realizado,
        'media_mensal': media_mensal,
        'crescimento': crescimento
    }

@app.route('/')
def index():
    # Página inicial do painel (menu de dashboards)
    return render_template('index.html')


# =============================================================================
# LOGIN, PERFIS DE ACESSO E ADMINISTRAÇÃO
# Usuários e perfis ficam em usuarios.py (SQLite local). Na primeira execução
# é criado o administrador com ADMIN_USER / ADMIN_PASS (padrão admin / admin).
# - Perfil administrador: tudo, inclusive /admin (usuários, perfis, banco).
# - Demais perfis: só as páginas do sistema marcadas no perfil.
# =============================================================================

app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
usuarios.iniciar()
banco_painel.iniciar()  # tabelas do schema painel e migração das planilhas (se o schema existir)

def _area_restrita(caminho):
    """Só /admin exige login; os painéis (e as APIs deles) são públicos."""
    return caminho == '/admin' or caminho.startswith('/admin/')


def _eh_requisicao_de_dados():
    return request.method != 'GET' or '/api/' in request.path or \
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _sem_permissao():
    if _eh_requisicao_de_dados():
        return jsonify({'ok': False, 'erro': 'Seu perfil não tem acesso a esta página.'}), 403
    return render_template('error.html', message='Seu perfil não tem acesso a esta página. '
                                                 'Fale com o administrador do painel.'), 403


@app.before_request
def controlar_acesso():
    """Carrega o usuário logado e verifica a permissão da rota pedida."""
    g.usuario = usuarios.obter_usuario(session['usuario_id']) if session.get('usuario_id') else None
    if g.usuario is not None and not g.usuario['ativo']:
        session.clear()
        g.usuario = None

    caminho = request.path
    if not _area_restrita(caminho):
        return None  # painéis, login e arquivos estáticos: públicos
    if g.usuario is None:
        if _eh_requisicao_de_dados():
            return jsonify({'ok': False, 'erro': 'Sessão expirada. Faça login novamente.'}), 401
        return redirect(url_for('login', proximo=caminho))

    # proteção contra envio forjado de formulário na área restrita
    if request.method == 'POST' and request.form and \
            request.form.get('csrf') != session.get('csrf'):
        return render_template('error.html', message='Formulário expirado. Recarregue a página e tente de novo.'), 400

    if caminho == '/admin':
        return None  # menu da área restrita: mostra só o que o perfil libera
    if caminho.startswith('/admin/banco'):
        return None if usuarios.eh_admin(g.usuario) else _sem_permissao()  # só o administrador
    chave = usuarios.pagina_da_rota(caminho)
    if chave is None:
        return None if usuarios.eh_admin(g.usuario) else _sem_permissao()
    return None if usuarios.pode_acessar(g.usuario, chave) else _sem_permissao()


@app.context_processor
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


@app.route('/login', methods=['GET', 'POST'])
def login():
    proximo = request.values.get('proximo')
    erro = None
    if request.method == 'POST':
        u = usuarios.autenticar(request.form.get('usuario'), request.form.get('senha'))
        if u:
            session.clear()
            session['usuario_id'] = u['id']
            session['csrf'] = secrets.token_hex(16)
            return redirect(_destino_seguro(proximo))
        erro = 'Usuário ou senha inválidos, ou usuário inativo.'
    return render_template('login.html', erro=erro, proximo=proximo or '')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/admin')
def admin_menu():
    return render_template('admin.html')


# Quem tem acesso a Usuários/Perfis mas não é administrador não pode criar,
# promover, editar nem excluir administradores (evita ganho de privilégio).
def _bloqueio_usuario(usuario_id, perfil_id=None):
    if usuarios.eh_admin(g.usuario):
        return None
    perfil = usuarios.obter_perfil(perfil_id) if perfil_id else None
    if perfil and perfil['admin']:
        return 'Só o administrador pode dar o perfil de administrador.'
    alvo = usuarios.obter_usuario(usuario_id) if usuario_id else None
    if alvo and alvo['perfil'] and alvo['perfil']['admin']:
        return 'Só o administrador pode alterar ou excluir um administrador.'
    return None


def _bloqueio_perfil(perfil_id, quer_admin=False):
    if usuarios.eh_admin(g.usuario):
        return None
    if quer_admin:
        return 'Só o administrador pode criar ou marcar um perfil como administrador.'
    perfil = usuarios.obter_perfil(perfil_id) if perfil_id else None
    if perfil and perfil['admin']:
        return 'Só o administrador pode alterar ou excluir o perfil de administrador.'
    return None


# ---- usuários ----

@app.route('/admin/usuarios')
def admin_usuarios():
    editar_id = request.args.get('editar', type=int)
    return render_template(
        'admin_usuarios.html',
        usuarios=usuarios.listar_usuarios(),
        perfis=usuarios.listar_perfis(),
        editando=usuarios.obter_usuario(editar_id) if editar_id else None,
    )


@app.route('/admin/usuarios/salvar', methods=['POST'])
def admin_usuarios_salvar():
    usuario_id = request.form.get('id', type=int)
    erro = _bloqueio_usuario(usuario_id, request.form.get('perfil_id', type=int)) or usuarios.salvar_usuario(
        usuario_id,
        request.form.get('login'), request.form.get('nome'), request.form.get('senha'),
        request.form.get('perfil_id', type=int), request.form.get('ativo') == '1',
    )
    if erro:
        flash(erro, 'erro')
        return redirect(url_for('admin_usuarios', editar=usuario_id) if usuario_id else url_for('admin_usuarios'))
    flash('Usuário salvo.', 'ok')
    return redirect(url_for('admin_usuarios'))


@app.route('/admin/usuarios/<int:usuario_id>/excluir', methods=['POST'])
def admin_usuarios_excluir(usuario_id):
    if usuario_id == g.usuario['id']:
        flash('Você não pode excluir o próprio usuário.', 'erro')
    else:
        erro = _bloqueio_usuario(usuario_id) or usuarios.excluir_usuario(usuario_id)
        flash(erro or 'Usuário excluído.', 'erro' if erro else 'ok')
    return redirect(url_for('admin_usuarios'))


# ---- perfis de acesso ----

@app.route('/admin/perfis')
def admin_perfis():
    editar_id = request.args.get('editar', type=int)
    return render_template(
        'admin_perfis.html',
        perfis=usuarios.listar_perfis(),
        paginas=usuarios.PAGINAS,
        nomes_paginas={chave: nome for chave, nome, _ in usuarios.PAGINAS},
        editando=usuarios.obter_perfil(editar_id) if editar_id else None,
    )


@app.route('/admin/perfis/salvar', methods=['POST'])
def admin_perfis_salvar():
    perfil_id = request.form.get('id', type=int)
    quer_admin = request.form.get('admin') == '1'
    erro = _bloqueio_perfil(perfil_id, quer_admin) or usuarios.salvar_perfil(
        perfil_id, request.form.get('nome'), quer_admin, request.form.getlist('paginas'),
    )
    if erro:
        flash(erro, 'erro')
        return redirect(url_for('admin_perfis', editar=perfil_id) if perfil_id else url_for('admin_perfis'))
    flash('Perfil salvo.', 'ok')
    return redirect(url_for('admin_perfis'))


@app.route('/admin/perfis/<int:perfil_id>/excluir', methods=['POST'])
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


@app.route('/admin/banco')
def admin_banco():
    cfg = config_banco.carregar()
    tem_senha = bool(cfg.pop('password'))  # a senha nunca volta para o navegador
    return render_template('config_banco.html', cfg=cfg, tem_senha=tem_senha,
                           erro_config=config_banco.erro_arquivo())


@app.route('/admin/banco/testar', methods=['POST'])
def admin_banco_testar():
    cfg = _config_do_formulario()
    erros = config_banco.validar(cfg)
    if erros:
        return jsonify({'ok': False, 'erro': ' '.join(erros)}), 400
    return jsonify(config_banco.testar(cfg))


@app.route('/admin/banco/salvar', methods=['POST'])
def admin_banco_salvar():
    cfg = _config_do_formulario()
    erros = config_banco.validar(cfg)
    if erros:
        return jsonify({'ok': False, 'erro': ' '.join(erros)}), 400
    try:
        config_banco.salvar(cfg)
    except OSError as e:
        return jsonify({'ok': False, 'erro': f'Não foi possível salvar: {e}'}), 500
    # banco ou schema dos dados podem ter mudado: refaz tabelas/migração no destino novo
    pronto = banco_painel.reiniciar()
    return jsonify({'ok': True, 'schema_painel_pronto': pronto, 'erro_schema_painel': banco_painel.erro()})

@app.route('/producao_hospitalar')
def producao_hospitalar():
    df = carregar_dados()
    if df.empty:
        return "Erro ao carregar dados. Verifique o arquivo Excel.", 500

    # Garantir que as colunas existem
    for col in ['Serviço', 'Exame/Procedimento', 'Tipo Procedimento', 'Quantidade', 'Mês Referência']:
        if col not in df.columns:
            return f"Coluna obrigatória ausente: {col}", 500

    # Limpeza e validação
    df['Serviço'] = df['Serviço'].fillna('').astype(str).str.strip()
    df['Exame/Procedimento'] = df['Exame/Procedimento'].fillna('').astype(str).str.strip()
    df['Tipo Procedimento'] = df['Tipo Procedimento'].fillna('').astype(str).str.strip()
    df['Quantidade'] = pd.to_numeric(df['Quantidade'], errors='coerce').fillna(0).astype(int)

    # Garantir data válida
    if not pd.api.types.is_datetime64_any_dtype(df['Mês Referência']):
        df['Mês Referência'] = pd.to_datetime(df['Mês Referência'], errors='coerce')

    df = df.dropna(subset=['Mês Referência'])

    # ISO para JS
    df['MesReferenciaISO'] = df['Mês Referência'].dt.strftime('%Y-%m-%d')

    # Listas únicas
    servicos = sorted([s for s in df['Serviço'].unique() if s])
    exames = sorted([e for e in df['Exame/Procedimento'].unique() if e])
    tipos = sorted([t for t in df['Tipo Procedimento'].unique() if t])

    # Datas disponíveis
    datas_disponiveis = sorted(df['Mês Referência'].dt.strftime('%Y-%m-%d').unique())
    data_inicial = datas_disponiveis[0] if datas_disponiveis else datetime.now().strftime('%Y-%m-%d')
    data_final = datas_disponiveis[-1] if datas_disponiveis else datetime.now().strftime('%Y-%m-%d')

    # KPIs
    kpis = calcular_kpis(df)

    # JSON frontend
    dados_json = df[['Mês Referência', 'Serviço', 'Tipo Procedimento', 'Exame/Procedimento', 'Quantidade']].copy()
    dados_json['Mês Referência'] = dados_json['Mês Referência'].dt.strftime('%Y-%m-%d')
    dados_json = dados_json.to_dict(orient='records')

    context = {
        'logo': get_encoded_logo(),
        'servicos': servicos,
        'exames_procedimentos': exames,
        'tipos_procedimento': tipos,
        'dados_json': dados_json,
        'data_inicial': data_inicial,
        'data_final': data_final,
        'datas_disponiveis': datas_disponiveis,
        'kpis': kpis
    }

    return render_template('producao_hospitalar.html', **context)

@app.route('/indicadores')
def indicadores():
    try:
        # tabela painel.simaq (a planilha original enquanto o schema painel não existir)
        linhas = banco_painel.SIMAQ.dados()
        df = pd.DataFrame({
            'Data': [f"{r['mes']}-01" for r in linhas],
            'Tipo': [r['tipo'] for r in linhas],
            'Indicador': [r['indicador'] for r in linhas],
            'Quantidade': [r['resultado'] for r in linhas],
            'Meta': [r['meta'] for r in linhas],
        })
        df['Data'] = pd.to_datetime(df['Data'], errors='coerce')
        df = df.dropna(subset=['Data'])
        df['Mês'] = df['Data'].dt.strftime('%Y-%m')
        df['Tipo'] = df['Tipo'].fillna('').astype(str).str.strip()
        df['Indicador'] = df['Indicador'].fillna('').astype(str).str.strip()
        df['Quantidade'] = pd.to_numeric(df['Quantidade'], errors='coerce').fillna(0)
        df['Meta'] = pd.to_numeric(df['Meta'], errors='coerce').fillna(0)

        meses = sorted(df['Mês'].unique())
        indicadores = sorted(df['Indicador'].unique())

        # Gera metas reais para todos os meses e indicadores presentes nos dados
        metas = []
        for indicador in indicadores:
            for mes in meses:
                meta_valor = df.loc[(df['Indicador'] == indicador) & (df['Mês'] == mes), 'Meta']
                meta = float(meta_valor.iloc[0]) if not meta_valor.empty else 0.0
                metas.append({'Mês': mes, 'Indicador': indicador, 'Meta': meta})

        # Garante que todos os meses/indicadores estejam presentes no JSON de dados (mesmo que quantidade seja 0)
        dados_completos = []
        for indicador in indicadores:
            for mes in meses:
                registros = df[(df['Indicador'] == indicador) & (df['Mês'] == mes)]
                if not registros.empty:
                    for _, row in registros.iterrows():
                        dados_completos.append({
                            'Data': row['Data'].strftime('%Y-%m-%d'),
                            'Tipo': row['Tipo'],
                            'Indicador': row['Indicador'],
                            'Quantidade': float(row['Quantidade']),
                            'Mês': mes
                        })
                else:
                    dados_completos.append({
                        'Data': f"{mes}-01",
                        'Tipo': '',
                        'Indicador': indicador,
                        'Quantidade': 0.0,
                        'Mês': mes
                    })

        kpis = {
            'total_quantidade': int(df['Quantidade'].sum()),
            'tipo_mais_relevante': df.groupby('Tipo')['Quantidade'].sum().idxmax() if not df['Tipo'].isnull().all() else ''
        }
        context = {
            'dados_json': dados_completos,
            'metas_json': metas,
            'tipos': sorted(df['Tipo'].unique()),
            'indicadores': indicadores,
            'data_inicial': df['Data'].min().strftime('%Y-%m-%d'),
            'data_final': df['Data'].max().strftime('%Y-%m-%d'),
            'kpis': kpis,
            'media_periodo': df['Quantidade'].mean()
        }
        return render_template('indicadores.html', **context)
    except Exception as e:
        return render_template('error.html', message=f"Erro ao carregar dados: {e}")

@app.route('/financeiro', methods=['GET', 'POST'])
def financeiro():
    caminho_planilha = os.path.join(PASTA_DADOS, 'DESPESAS EMPENHADAS.xlsx')
    try:
        df_original = pd.read_excel(
            caminho_planilha,
            header=0,
            dtype=str,
            engine='openpyxl'
        )

        # Ajuste robusto para encontrar as colunas, independentemente de acentuação/capitalização
        def encontrar_coluna(df, nomes_possiveis):
            for nome in df.columns:
                nome_normalizado = nome.strip().lower().replace('ã', 'a').replace('é', 'e').replace('ê', 'e').replace('í', 'i').replace('ó', 'o').replace('ú', 'u').replace('ç', 'c').replace('ô', 'o')
                for alvo in nomes_possiveis:
                    alvo_normalizado = alvo.strip().lower().replace('ã', 'a').replace('é', 'e').replace('ê', 'e').replace('í', 'i').replace('ó', 'o').replace('ú', 'u').replace('ç', 'c').replace('ô', 'o')
                    if nome_normalizado == alvo_normalizado:
                        return nome
            return None

        # Defina os nomes possíveis para cada coluna
        nomes_mes = ['Mês', 'Mes']
        nomes_fonte = ['Fonte do Recurso', 'Fonte', 'Fonte Recurso']
        nomes_natureza = ['Natureza da Despesa', 'Natureza', 'Natureza Despesa']
        nomes_despesa = ['Despesa']
        nomes_valor = ['Valor R$ no mês', 'Valor R$ no Mês', 'Valor', 'Valor R$', 'Valor no mês', 'Valor no Mês']

        col_mes = encontrar_coluna(df_original, nomes_mes)
        col_fonte = encontrar_coluna(df_original, nomes_fonte)
        col_natureza = encontrar_coluna(df_original, nomes_natureza)
        col_despesa = encontrar_coluna(df_original, nomes_despesa)
        col_valor = encontrar_coluna(df_original, nomes_valor)

        # Verifica se todas as colunas necessárias foram encontradas
        for nome, col in [('Mês', col_mes), ('Fonte do Recurso', col_fonte), ('Natureza da Despesa', col_natureza), ('Despesa', col_despesa), ('Valor R$ no mês', col_valor)]:
            if col is None:
                raise Exception(f"Coluna '{nome}' não encontrada na planilha.")

        # Renomeia as colunas para nomes padronizados
        df_original = df_original.rename(columns={
            col_mes: 'Mês',
            col_fonte: 'Fonte do Recurso',
            col_natureza: 'Natureza da Despesa',
            col_despesa: 'Despesa',
            col_valor: 'Valor R$ no mês'
        })

        # Use sempre 'Valor R$ no mês' (com "mês" minúsculo) como nome padrão
        # Ajuste todas as ocorrências abaixo para usar 'Valor R$ no mês'
        def normalizar_valor(valor):
            if isinstance(valor, str):
                valor = valor.replace('R$', '').replace(' ', '').replace('.', '').replace(',', '.')
                try:
                    return float(valor)
                except Exception:
                    return 0.0
            try:
                return float(valor)
            except Exception:
                return 0.0

        df_original['Valor R$ no mês'] = df_original['Valor R$ no mês'].apply(normalizar_valor)
        df_original['Mês'] = pd.to_datetime(df_original['Mês'], format='%Y-%m', errors='coerce')
        if df_original['Mês'].isnull().all():
            df_original['Mês'] = pd.to_datetime(df_original['Mês'], errors='coerce')
        df_original = df_original.dropna(subset=['Mês', 'Valor R$ no mês'])

        # Sempre define o padr�o como os �ltimos 12 meses dispon�veis
        if not df_original.empty:
            data_final_padrao = df_original['Mês'].max()
            data_inicial_padrao = (data_final_padrao - pd.DateOffset(months=12) + pd.DateOffset(days=1)).replace(day=1)
        else:
            data_final_padrao = datetime.now()
            data_inicial_padrao = data_final_padrao - timedelta(days=365)

        if request.method == 'POST':
            filtros = request.get_json()
            data_inicial = pd.to_datetime(filtros.get('data_inicial'), errors='coerce')
            data_final = pd.to_datetime(filtros.get('data_final'), errors='coerce')
            if pd.isnull(data_inicial):
                data_inicial = data_inicial_padrao
            if pd.isnull(data_final):
                data_final = data_final_padrao
            fonte_despesa = filtros.get('fonte_despesa')
            natureza_despesa = filtros.get('natureza_despesa')
            despesa = filtros.get('despesa')
        else:
            data_inicial = data_inicial_padrao
            data_final = data_final_padrao
            fonte_despesa = None
            natureza_despesa = None
            despesa = None

        fontes_despesa = sorted(df_original['Fonte do Recurso'].dropna().unique())
        naturezas_despesa = sorted(df_original['Natureza da Despesa'].dropna().unique())
        despesas = sorted(df_original['Despesa'].dropna().unique())

        df = df_original.copy()
        df = df[(df['Mês'] >= data_inicial) & (df['Mês'] <= data_final)]
        if fonte_despesa and fonte_despesa != 'todos':
            df = df[df['Fonte do Recurso'] == fonte_despesa]
        if natureza_despesa and natureza_despesa != 'todos':
            df = df[df['Natureza da Despesa'] == natureza_despesa]
        if despesa and despesa != 'todos':
            df = df[df['Despesa'] == despesa]

        valor_total = float(df['Valor R$ no mês'].sum()) if not df.empty else 0.0
        if not df.empty and df['Valor R$ no mês'].max() > 0:
            maior_despesa_row = df.loc[df['Valor R$ no mês'].idxmax()]
            maior_despesa = {
                'descricao': str(maior_despesa_row['Despesa']),
                'valor': float(maior_despesa_row['Valor R$ no mês'])
            }
        else:
            maior_despesa = {'descricao': '', 'valor': 0.0}
        valor_medio = float(df['Valor R$ no mês'].mean()) if not df.empty else 0.0

        # Dados para gráfico: garantir todos os meses no Período, mesmo sem despesa
        if not df.empty:
            date_range = pd.date_range(start=data_inicial, end=data_final, freq='MS')
            all_months = date_range.strftime('%Y-%m').tolist()
            all_despesas = sorted(set(df['Despesa'].dropna().astype(str).unique()))
            df['MesReferencia'] = df['Mês'].dt.strftime('%Y-%m')

            # Gera DataFrame base com todos os meses e todas as despesas
            base = pd.MultiIndex.from_product([all_months, all_despesas], names=['MesReferencia', 'Despesa'])
            df_grouped = (
                df.groupby(['MesReferencia', 'Despesa'])['Valor R$ no mês']
                .sum()
                .reindex(base, fill_value=0)
                .reset_index()
            )

            # Pivot para formato de gráfico empilhado
            despesas_por_mes_df = df_grouped.pivot(index='MesReferencia', columns='Despesa', values='Valor R$ no mês').fillna(0)
            despesas_por_mes_df = despesas_por_mes_df.reset_index()

            # Adiciona coluna Total
            despesas_por_mes_df['Total'] = despesas_por_mes_df[all_despesas].sum(axis=1)

            # Garante que todos os meses estejam presentes e ordenados
            despesas_por_mes_df = despesas_por_mes_df.set_index('MesReferencia').reindex(all_months, fill_value=0).reset_index()

            despesas_por_mes = despesas_por_mes_df.to_dict(orient='list')
        else:
            despesas_por_mes = {}

        if request.method == 'POST':
            return jsonify({
                'valor_total': valor_total,
                'maior_despesa': maior_despesa,
                'valor_medio': valor_medio,
                'despesas_por_mes': despesas_por_mes
            })

        context = {
            'data_inicial': data_inicial.strftime('%Y-%m-%d') if pd.notnull(data_inicial) else '',
            'data_final': data_final.strftime('%Y-%m-%d') if pd.notnull(data_final) else '',
            'fontes_despesa': fontes_despesa,
            'naturezas_despesa': naturezas_despesa,
            'despesas': despesas,
            'valor_total': valor_total,
            'maior_despesa': maior_despesa,
            'valor_medio': valor_medio,
            'despesas_por_mes': despesas_por_mes
        }
        return render_template('financeiro.html', **context)
    except Exception as e:
        return f"Erro ao carregar dados financeiros: {e}", 500

@app.route('/faturamento', methods=['GET', 'POST'])
def faturamento():
    caminho_planilha = os.path.join(PASTA_DADOS, 'FATURAMENTO CONTAS MEDICAS.xlsx')
    try:
        df_original = pd.read_excel(
            caminho_planilha,
            header=0,
            dtype=str,
            engine='openpyxl'
        )
        # Ajuste robusto para encontrar as colunas, independentemente de acentuação/capitalização
        def encontrar_coluna(df, nomes_possiveis):
            for nome in df.columns:
                nome_normalizado = (
                    nome.strip()
                    .lower()
                    .replace('ã', 'a').replace('á', 'a').replace('â', 'a')
                    .replace('é', 'e').replace('ê', 'e')
                    .replace('í', 'i')
                    .replace('ó', 'o').replace('ô', 'o').replace('õ', 'o')
                    .replace('ú', 'u')
                    .replace('ç', 'c')
                )
                for alvo in nomes_possiveis:
                    alvo_normalizado = (
                        alvo.strip()
                        .lower()
                        .replace('ã', 'a').replace('á', 'a').replace('â', 'a')
                        .replace('é', 'e').replace('ê', 'e')
                        .replace('í', 'i')
                        .replace('ó', 'o').replace('ô', 'o').replace('õ', 'o')
                        .replace('ú', 'u')
                        .replace('ç', 'c')
                    )
                    if nome_normalizado == alvo_normalizado:
                        return nome
            return None

        # Defina os nomes possíveis para cada coluna
        nomes_data = ['MÊS', 'MES', 'Data', 'DATA', 'A', 'Dt', 'dt', 'data']
        nomes_tipo = ['TIPO', 'Tipo', 'B', 'tipo']
        nomes_desc = ['DESCRIÇÃO TIPO', 'Descrição', 'Descricao', 'DESCRIÇÃO', 'DESCRICAO', 'C', 'descricao', 'descrição']
        nomes_valor = ['VALOR', 'Valor', 'D', 'valor']

        col_data = encontrar_coluna(df_original, nomes_data)
        col_tipo = encontrar_coluna(df_original, nomes_tipo)
        col_desc = encontrar_coluna(df_original, nomes_desc)
        col_valor = encontrar_coluna(df_original, nomes_valor)

        # Verifica se todas as colunas necessárias foram encontradas
        for nome, col in [('MÊS', col_data), ('Tipo', col_tipo), ('Descricao', col_desc), ('Valor', col_valor)]:
            if col is None:
                raise Exception(f"Coluna '{nome}' não encontrada na planilha. Colunas encontradas: {list(df_original.columns)}")

        # Renomeia as colunas para nomes padronizados
        df_original = df_original.rename(columns={
            col_data: 'MÊS',
            col_tipo: 'Tipo',
            col_desc: 'Descricao',
            col_valor: 'Valor'
        })

        # Converte 'MÊS' para datetime (esperado formato %Y-%m ou %d/%m/%Y)
        df_original['MÊS'] = pd.to_datetime(df_original['MÊS'], errors='coerce', format='%Y-%m')
        if df_original['MÊS'].isnull().all():
            df_original['MÊS'] = pd.to_datetime(df_original['MÊS'], errors='coerce')

        # Normaliza valores
        df_original['Valor'] = df_original['Valor'].apply(
            lambda v: float(str(v).replace('R$', '').replace(' ', '').replace('.', '').replace(',', '.')) if pd.notnull(v) else 0.0
        )
        df_original = df_original.dropna(subset=['MÊS', 'Valor'])

        # Datas padrão: últimos 12 meses disponíveis
        if not df_original.empty:
            data_final_padrao = df_original['MÊS'].max()
            data_inicial_padrao = (data_final_padrao - pd.DateOffset(months=12) + pd.DateOffset(days=1)).replace(day=1)
        else:
            data_final_padrao = datetime.now()
            data_inicial_padrao = data_final_padrao - timedelta(days=365)

        if request.method == 'POST':
            filtros = request.get_json()
            data_inicial = pd.to_datetime(filtros.get('data_inicial'), errors='coerce')
            data_final = pd.to_datetime(filtros.get('data_final'), errors='coerce')
            if pd.isnull(data_inicial):
                data_inicial = data_inicial_padrao
            if pd.isnull(data_final):
                data_final = data_final_padrao
            tipo = filtros.get('tipo')
            descricao = filtros.get('descricao')
        else:
            data_inicial = data_inicial_padrao
            data_final = data_final_padrao
            tipo = None
            descricao = None

        # Dropdowns SEM filtro
        tipos = sorted(df_original['Tipo'].dropna().unique())
        descricoes = sorted(df_original['Descricao'].dropna().unique())

        # Aplica filtros
        df = df_original.copy()
        # Troque 'Data' por 'MÊS' para o filtro de datas e para o gráfico
        df = df[(df['MÊS'] >= data_inicial) & (df['MÊS'] <= data_final)]
        if tipo and tipo != 'todos':
            df = df[df['Tipo'] == tipo]
        if descricao and descricao != 'todos':
            df = df[df['Descricao'] == descricao]

        # KPIs
        valor_total = float(df['Valor'].sum()) if not df.empty else 0.0
        if not df.empty and df['Valor'].max() > 0:
            maior_row = df.loc[df['Valor'].idxmax()]
            maior = {
                'descricao': str(maior_row['Descricao']),
                'valor': float(maior_row['Valor'])
            }
        else:
            maior = {'descricao': '', 'valor': 0.0}
        valor_medio = float(df['Valor'].mean()) if not df.empty else 0.0

        # Dados para gráfico: garantir todos os meses no período, mesmo sem valor
        if not df.empty:
            date_range = pd.date_range(start=data_inicial, end=data_final, freq='MS')
            all_months = date_range.strftime('%Y-%m').tolist()
            all_descricoes = sorted(set(df['Descricao'].dropna().astype(str).unique()))
            df['MesReferencia'] = df['MÊS'].dt.strftime('%Y-%m')

            base = pd.MultiIndex.from_product([all_months, all_descricoes], names=['MesReferencia', 'Descricao'])
            df_grouped = (
                df.groupby(['MesReferencia', 'Descricao'])['Valor']
                .sum()
                .reindex(base, fill_value=0)
                .reset_index()
            )

            # Pivot para gráfico empilhado
            valores_por_mes_df = df_grouped.pivot(index='MesReferencia', columns='Descricao', values='Valor').fillna(0)
            valores_por_mes_df = valores_por_mes_df.reset_index()
            valores_por_mes_df['Total'] = valores_por_mes_df[all_descricoes].sum(axis=1)
            valores_por_mes_df = valores_por_mes_df.set_index('MesReferencia').reindex(all_months, fill_value=0).reset_index()
            valores_por_mes = valores_por_mes_df.to_dict(orient='list')
        else:
            valores_por_mes = {}

        if request.method == 'POST':
            return jsonify({
                'valor_total': valor_total,
                'maior': maior,
                'valor_medio': valor_medio,
                'valores_por_mes': valores_por_mes
            })

        context = {
            'data_inicial': data_inicial.strftime('%Y-%m-%d') if pd.notnull(data_inicial) else '',
            'data_final': data_final.strftime('%Y-%m-%d') if pd.notnull(data_final) else '',
            'tipos': tipos,
            'descricoes': descricoes,
            'valor_total': valor_total,
            'maior': maior,
            'valor_medio': valor_medio,
            'valores_por_mes': valores_por_mes
        }
        return render_template('faturamento.html', **context)
    except Exception as e:
        return f"Erro ao carregar dados de faturamento: {e}", 500

@app.route('/teste')
def teste():
    print("Rota de teste acessada")
    return render_template('teste.html', mensagem="Teste funcionando!", 
                          lista_teste=["Item 1", "Item 2", "Item 3"])

@app.route('/voltar_inicio')
def voltar_inicio():
    # Redireciona para a página inicial do painel
    return render_template('index.html')

@app.route('/exames_por_clinica', methods=['GET'])
def exames_por_clinica():
    hoje = datetime.now().date()
    primeiro_dia_mes = hoje.replace(day=1)

    try:
        data_inicial = datetime.strptime(request.args.get('data_inicial', ''), '%Y-%m-%d').date()
    except ValueError:
        data_inicial = primeiro_dia_mes
    try:
        data_final = datetime.strptime(request.args.get('data_final', ''), '%Y-%m-%d').date()
    except ValueError:
        data_final = hoje

    clinica_filtro = request.args.get('clinica') or 'todos'
    classificacao_filtro = request.args.get('classificacao') or 'todos'

    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT dsc_clinica FROM t_clinica ORDER BY dsc_clinica")
        clinicas = [r['dsc_clinica'] for r in cur.fetchall()]

        cur.execute("""
            SELECT DISTINCT classificacao FROM t_exame_classificacao
            WHERE classificacao IS NOT NULL ORDER BY classificacao
        """)
        classificacoes = [r['classificacao'] for r in cur.fetchall()]

        id_ini, id_fim = _faixa_ids_exame_solic(cur, data_inicial, data_final)
        params = [data_inicial, data_final, id_ini, id_fim]
        filtro_clinica_sql = ""
        if clinica_filtro != 'todos' and clinica_filtro in clinicas:
            filtro_clinica_sql = "AND tc.dsc_clinica = %s"
            params.append(clinica_filtro)

        filtro_classificacao_sql = ""
        if classificacao_filtro != 'todos' and classificacao_filtro in classificacoes:
            filtro_classificacao_sql = "AND tec.classificacao = %s"
            params.append(classificacao_filtro)

        cur.execute(f"""
            SELECT
                tc.dsc_clinica AS clinica,
                CASE
                    WHEN tc.isn_clinica IN ({_EXAMES_ORIGEM_EXTERNO_SQL}) THEN 'externo'
                    WHEN tc.isn_clinica IN ({_EXAMES_ORIGEM_EMERGENCIA_SQL}) THEN 'emergencia'
                    ELSE 'interno'
                END AS origem,
                te.dsc_exame AS exame,
                COALESCE(tec.classificacao, 'SEM CLASSIFICAÇÃO') AS classificacao,
                COUNT(*) AS solicitados,
                COUNT(*) FILTER (WHERE tes.dat_agendamento IS NOT NULL) AS baixados,
                COUNT(*) FILTER (WHERE tes.dat_realizacao IS NOT NULL) AS realizados,
                COUNT(*) FILTER (WHERE tes.dat_cancelamento IS NOT NULL) AS cancelados,
                -- tempo da solicitação até a realização (a baixa, dat_agendamento,
                -- só tem data). Faixas sem sobreposição; realização registrada
                -- antes da solicitação não entra em nenhuma.
                COUNT(*) FILTER (WHERE tes.dat_realizacao >= tes.dat_solicitacao
                                   AND tes.dat_realizacao - tes.dat_solicitacao <= INTERVAL '1 day') AS tempo_ate_1d,
                COUNT(*) FILTER (WHERE tes.dat_realizacao - tes.dat_solicitacao > INTERVAL '1 day'
                                   AND tes.dat_realizacao - tes.dat_solicitacao <= INTERVAL '2 days') AS tempo_1_2d,
                COUNT(*) FILTER (WHERE tes.dat_realizacao - tes.dat_solicitacao > INTERVAL '2 days'
                                   AND tes.dat_realizacao - tes.dat_solicitacao <= INTERVAL '4 days') AS tempo_2_4d,
                COUNT(*) FILTER (WHERE tes.dat_realizacao - tes.dat_solicitacao > INTERVAL '4 days') AS tempo_acima_4d,
                -- tempo da realização até a baixa, em dias de calendário
                -- (dat_agendamento é só data): diferença entre as datas.
                -- Baixa registrada antes da realização não entra em nenhuma faixa.
                COUNT(*) FILTER (WHERE tes.dat_agendamento IS NOT NULL AND tes.dat_realizacao IS NOT NULL) AS com_realizacao_e_baixa,
                COUNT(*) FILTER (WHERE tes.dat_agendamento - tes.dat_realizacao::date BETWEEN 0 AND 1) AS baixa_ate_1d,
                COUNT(*) FILTER (WHERE tes.dat_agendamento - tes.dat_realizacao::date = 2) AS baixa_1_2d,
                COUNT(*) FILTER (WHERE tes.dat_agendamento - tes.dat_realizacao::date BETWEEN 3 AND 4) AS baixa_2_4d,
                COUNT(*) FILTER (WHERE tes.dat_agendamento - tes.dat_realizacao::date > 4) AS baixa_acima_4d,
                COUNT(*) FILTER (WHERE tes.dat_agendamento IS NULL) AS sem_agendamento
            FROM t_exame_solic tes
            JOIN t_clinica tc ON tc.isn_clinica = tes.isn_clinica
            JOIN t_exame te ON te.isn_exame = tes.isn_exame
            LEFT JOIN t_exame_classificacao tec ON tec.isn_exame = tes.isn_exame
            WHERE tes.dat_solicitacao::date BETWEEN %s AND %s
              AND tes.isn_exame_solic >= %s AND tes.isn_exame_solic < %s
              {filtro_clinica_sql}
              {filtro_classificacao_sql}
            GROUP BY tc.dsc_clinica, origem, te.dsc_exame, tec.classificacao
            ORDER BY tc.dsc_clinica, te.dsc_exame
        """, params)
        detalhe_rows = cur.fetchall()

        cur.close()
        conn.close()
    except Exception as e:
        return render_template('error.html', message=f"Erro ao carregar dados de exames por clínica: {e}")

    dados_json = [
        {
            'clinica': r['clinica'],
            'origem': r['origem'],
            'exame': r['exame'],
            'classificacao': r['classificacao'],
            'solicitados': int(r['solicitados']),
            'baixados': int(r['baixados']),
            'realizados': int(r['realizados']),
            'cancelados': int(r['cancelados']),
            'tempo_ate_1d': int(r['tempo_ate_1d']),
            'tempo_1_2d': int(r['tempo_1_2d']),
            'tempo_2_4d': int(r['tempo_2_4d']),
            'tempo_acima_4d': int(r['tempo_acima_4d']),
            'com_realizacao_e_baixa': int(r['com_realizacao_e_baixa']),
            'baixa_ate_1d': int(r['baixa_ate_1d']),
            'baixa_1_2d': int(r['baixa_1_2d']),
            'baixa_2_4d': int(r['baixa_2_4d']),
            'baixa_acima_4d': int(r['baixa_acima_4d']),
            'sem_agendamento': int(r['sem_agendamento']),
        }
        for r in detalhe_rows
    ]

    resumo_por_clinica = {}
    for r in dados_json:
        acc = resumo_por_clinica.setdefault(r['clinica'], {
            'clinica': r['clinica'], 'solicitados': 0, 'baixados': 0, 'realizados': 0, 'cancelados': 0
        })
        acc['solicitados'] += r['solicitados']
        acc['baixados'] += r['baixados']
        acc['realizados'] += r['realizados']
        acc['cancelados'] += r['cancelados']

    resumo_json = sorted(resumo_por_clinica.values(), key=lambda x: x['clinica'])

    total_geral = {
        'clinica': 'TOTAL GERAL',
        'solicitados': sum(r['solicitados'] for r in resumo_json),
        'baixados': sum(r['baixados'] for r in resumo_json),
        'realizados': sum(r['realizados'] for r in resumo_json),
        'cancelados': sum(r['cancelados'] for r in resumo_json),
    }
    resumo_json.append(total_geral)

    resumo_por_classificacao = {}
    for r in dados_json:
        acc = resumo_por_classificacao.setdefault(r['classificacao'], {
            'classificacao': r['classificacao'], 'solicitados': 0, 'baixados': 0, 'realizados': 0, 'cancelados': 0
        })
        acc['solicitados'] += r['solicitados']
        acc['baixados'] += r['baixados']
        acc['realizados'] += r['realizados']
        acc['cancelados'] += r['cancelados']

    resumo_classificacao_json = sorted(resumo_por_classificacao.values(), key=lambda x: x['classificacao'])
    resumo_classificacao_json.append({
        'classificacao': 'TOTAL GERAL',
        'solicitados': total_geral['solicitados'],
        'baixados': total_geral['baixados'],
        'realizados': total_geral['realizados'],
        'cancelados': total_geral['cancelados'],
    })

    kpis = {
        'total_solicitados': total_geral['solicitados'],
        'total_baixados': total_geral['baixados'],
        'total_realizados': total_geral['realizados'],
        'total_cancelados': total_geral['cancelados'],
        'total_sem_agendamento': sum(r['sem_agendamento'] for r in dados_json),
    }
    # média diária de cada total (cards de média abaixo dos totais)
    dias_no_periodo = max(1, (data_final - data_inicial).days + 1)
    for chave in list(kpis):
        kpis[chave.replace('total_', 'media_')] = round(kpis[chave] / dias_no_periodo, 1)

    context = {
        'data_inicial': data_inicial.strftime('%Y-%m-%d'),
        'data_final': data_final.strftime('%Y-%m-%d'),
        'clinicas': clinicas,
        'classificacoes': classificacoes,
        'classificacao_selecionada': classificacao_filtro,
        'kpis': kpis,
        'dados_json': dados_json,
        'resumo_json': resumo_json,
        'resumo_classificacao_json': resumo_classificacao_json,
        'total_geral': total_geral,
    }

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(context)

    return render_template('exames_por_clinica.html', **context)



@app.route('/graficos_mensais', methods=['GET'])
def graficos_mensais():
    # Padrão: os 12 últimos meses completos, sem o mês corrente.
    hoje = datetime.now().date()
    fim_mes_anterior = hoje.replace(day=1) - timedelta(days=1)
    primeiro_dia_12meses = (fim_mes_anterior.replace(day=1) - pd.DateOffset(months=11)).date()

    try:
        data_inicial = datetime.strptime(request.args.get('data_inicial', ''), '%Y-%m-%d').date()
    except ValueError:
        data_inicial = primeiro_dia_12meses
    try:
        data_final = datetime.strptime(request.args.get('data_final', ''), '%Y-%m-%d').date()
    except ValueError:
        data_final = fim_mes_anterior

    clinica_filtro = request.args.get('clinica') or 'todos'
    classificacao_filtro = request.args.get('classificacao') or 'todos'
    exame_filtro = request.args.get('exame') or 'todos'

    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT dsc_clinica FROM t_clinica ORDER BY dsc_clinica")
        clinicas = [r['dsc_clinica'] for r in cur.fetchall()]

        cur.execute("""
            SELECT DISTINCT classificacao FROM t_exame_classificacao
            WHERE classificacao IS NOT NULL ORDER BY classificacao
        """)
        classificacoes = [r['classificacao'] for r in cur.fetchall()]

        cur.execute("""
            SELECT DISTINCT dsc_exame FROM t_exame ORDER BY dsc_exame
        """)
        exames = [r['dsc_exame'] for r in cur.fetchall()]

        params = [data_inicial, data_final]
        filtros_sql = ""

        if clinica_filtro != 'todos' and clinica_filtro in clinicas:
            filtros_sql += " AND tc.dsc_clinica = %s"
            params.append(clinica_filtro)

        if classificacao_filtro != 'todos' and classificacao_filtro in classificacoes:
            filtros_sql += " AND tec.classificacao = %s"
            params.append(classificacao_filtro)

        if exame_filtro != 'todos' and exame_filtro in exames:
            filtros_sql += " AND te.dsc_exame = %s"
            params.append(exame_filtro)

        # Já agrupado por mês × classificação, que é o que os gráficos mostram
        # (antes vinha uma linha por dia × clínica × classificação × exame, dezenas
        # de MB para 12 meses). Todos os indicadores pelo mês da solicitação.
        cur.execute(f"""
            SELECT
                DATE_TRUNC('month', tes.dat_solicitacao)::date AS mes,
                COALESCE(tec.classificacao, 'SEM CLASSIFICAÇÃO') AS classificacao,
                COUNT(*) AS solicitados,
                COUNT(*) FILTER (WHERE tes.dat_agendamento IS NOT NULL) AS baixados,
                COUNT(*) FILTER (WHERE tes.dat_realizacao IS NOT NULL) AS realizados,
                COUNT(*) FILTER (WHERE tes.dat_cancelamento IS NOT NULL) AS cancelados,
                COUNT(*) FILTER (WHERE tes.dat_agendamento IS NULL) AS sem_agendamento
            FROM t_exame_solic tes
            JOIN t_clinica tc ON tc.isn_clinica = tes.isn_clinica
            JOIN t_exame te ON te.isn_exame = tes.isn_exame
            LEFT JOIN t_exame_classificacao tec ON tec.isn_exame = tes.isn_exame
            WHERE tes.dat_solicitacao::date BETWEEN %s AND %s
              {filtros_sql}
            GROUP BY 1, 2
        """, params)
        rows = cur.fetchall()

        cur.close()
        conn.close()
    except Exception as e:
        return render_template('error.html', message=f"Erro ao carregar dados dos gráficos mensais: {e}")

    metricas = ['solicitados', 'baixados', 'realizados', 'cancelados', 'sem_agendamento']

    # Todos os meses do período (inclusive os sem nenhum exame), em ordem.
    meses = []
    mes = data_inicial.replace(day=1)
    while mes <= data_final:
        meses.append(mes)
        mes = (mes.replace(day=28) + timedelta(days=4)).replace(day=1)
    idx_mes = {m: i for i, m in enumerate(meses)}

    classificacoes_graf = sorted({r['classificacao'] for r in rows})
    series = {m: {c: [0] * len(meses) for c in classificacoes_graf} for m in metricas}
    for r in rows:
        i = idx_mes.get(r['mes'])
        if i is None:
            continue
        for m in metricas:
            series[m][r['classificacao']][i] += int(r[m])

    total_geral = {m: sum(sum(v) for v in series[m].values()) for m in metricas}

    kpis = {
        'total_solicitados': total_geral['solicitados'],
        'total_baixados': total_geral['baixados'],
        'total_realizados': total_geral['realizados'],
        'total_cancelados': total_geral['cancelados'],
        'total_sem_agendamento': total_geral['sem_agendamento'],
    }
    # média mensal de cada total (cards de média abaixo dos totais)
    for chave in list(kpis):
        kpis[chave.replace('total_', 'media_')] = round(kpis[chave] / max(1, len(meses)), 1)

    context = {
        'data_inicial': data_inicial.strftime('%Y-%m-%d'),
        'data_final': data_final.strftime('%Y-%m-%d'),
        'clinicas': clinicas,
        'classificacoes': classificacoes,
        'exames': exames,
        'clinica_selecionada': clinica_filtro,
        'classificacao_selecionada': classificacao_filtro,
        'exame_selecionado': exame_filtro,
        'kpis': kpis,
        'graficos': {
            'meses': [m.strftime('%m/%Y') for m in meses],
            'classificacoes': classificacoes_graf,
            'series': series,
        },
        'total_geral': total_geral,
    }

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(context)

    return render_template('graficos_mensais.html', **context)

# ---------------------------------------------------------------------------
# INDICADORES HCASG — Hospitalares e Emergência (tendência mensal + comparativo)
#
# Cole o import abaixo junto com os demais imports do topo do app.py:
#
#   from indicadores_hcasg import get_filtros as hcasg_get_filtros
#   from indicadores_hcasg import get_mensal_hospitalar, get_mensal_emergencia
#   from indicadores_hcasg import get_indicadores_hospitalar, get_indicadores_emergencia
#
# E cole o bloco de rotas abaixo em qualquer lugar entre as outras rotas
# (@app.route) e o `if __name__ == '__main__':` final do app.py.
# ---------------------------------------------------------------------------

HCASG_CAMPOS_HOSPITALAR = [
    ("paciente_dia", "Paciente-dia", "dias"),
    ("saidas", "Saídas", "qtd"),
    ("tmph", "Tempo Médio de Permanência", "dias"),
    ("giro", "Giro de Leito", "saídas/leito"),
    ("leito_dia", "Leito-Dia", "leitos-dia"),
    ("taxa_ocupacao", "Taxa de Ocupação", "%"),
    ("total_obitos", "Óbitos (total)", "qtd"),
    ("obitos_institucionais", "Óbitos Institucionais", "qtd"),
    ("taxa_mortalidade_hospitalar", "Taxa de Mortalidade Hospitalar", "%"),
    ("taxa_mortalidade_institucional", "Taxa de Mortalidade Institucional", "%"),
]

HCASG_CAMPOS_EMERGENCIA = [
    ("los_sem", "LOS Sem Internação", "min"),
    ("los_com", "LOS Com Internação", "min"),
    ("tmp_ps", "TMP Leitos PS", "dias"),
    ("saidas_tmp_ps", "Saídas PS", "qtd"),
    ("giro_ps", "Giro de Leito PS", "saídas/leito"),
    ("porta_medico", "Tempo Porta-Médico", "min"),
    ("internacoes_ps", "Internações PS", "qtd"),
]


def _hcasg_variacao(valor_a, valor_b):
    """Retorna o percentual de variação de A para B, ou None se não for possível calcular."""
    try:
        if valor_a is None or valor_b is None:
            return None
        va = float(valor_a)
        vb = float(valor_b)
        if va == 0:
            return None
        return round((vb - va) / va * 100, 2)
    except (TypeError, ValueError):
        return None


def _hcasg_montar_comparativo(dados, campos, mes_a, mes_b):
    meses_iso = dados.get("meses_iso") or []
    if mes_a not in meses_iso or mes_b not in meses_iso:
        return None
    idx_a = meses_iso.index(mes_a)
    idx_b = meses_iso.index(mes_b)
    linhas = []
    for chave, rotulo, unidade in campos:
        serie = dados.get(chave) or []
        valor_a = serie[idx_a] if idx_a < len(serie) else None
        valor_b = serie[idx_b] if idx_b < len(serie) else None
        linhas.append({
            "chave": chave,
            "indicador": rotulo,
            "unidade": unidade,
            "valor_a": valor_a,
            "valor_b": valor_b,
            "variacao": _hcasg_variacao(valor_a, valor_b),
        })
    return {
        "mes_a_label": dados["meses"][idx_a],
        "mes_b_label": dados["meses"][idx_b],
        "linhas": linhas,
    }


def _hcasg_params_filtro():
    clinica = request.args.getlist("clinica") or None
    if clinica and len(clinica) == 1:
        clinica = clinica[0]
    enfermaria = request.args.get("enfermaria") or None
    leito = request.args.get("leito") or None
    return clinica, enfermaria, leito


# ---- Páginas (agora páginas de indicadores "ao vivo", como o painel LEAN,
#      só que cada uma restrita ao seu bloco) ----

@app.route('/hcasg/hospitalares')
def hcasg_hospitalares():
    clinicas, enfermarias, leitos = hcasg_get_filtros()
    hoje = datetime.now().date()
    primeiro_dia_mes = hoje.replace(day=1)
    return render_template(
        'hcasg_hospitalares.html',
        clinicas=clinicas, enfermarias=enfermarias, leitos=leitos,
        data_ini=primeiro_dia_mes.strftime('%Y-%m-%d'), data_fim=hoje.strftime('%Y-%m-%d'),
    )


@app.route('/hcasg/emergencia')
def hcasg_emergencia():
    clinicas, enfermarias, leitos = hcasg_get_filtros()
    hoje = datetime.now().date()
    primeiro_dia_mes = hoje.replace(day=1)
    return render_template(
        'hcasg_emergencia.html',
        clinicas=clinicas, enfermarias=enfermarias, leitos=leitos,
        data_ini=primeiro_dia_mes.strftime('%Y-%m-%d'), data_fim=hoje.strftime('%Y-%m-%d'),
    )


@app.route('/api/hcasg/hospitalares/indicadores')
def api_hcasg_hospitalares_indicadores():
    clinica, enfermaria, leito = _hcasg_params_filtro()
    data_ini = request.args.get('data_ini')
    data_fim = request.args.get('data_fim')
    try:
        dados = get_indicadores_hospitalar(data_ini, data_fim, clinica, enfermaria, leito)
        return jsonify({'ok': True, 'dados': dados})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


@app.route('/ambulatorio')
def ambulatorio_pagina():
    hoje = datetime.now().date()
    return render_template(
        'ambulatorio.html',
        data_ini=hoje.replace(day=1).strftime('%Y-%m-%d'), data_fim=hoje.strftime('%Y-%m-%d'),
    )


@app.route('/api/ambulatorio/dados')
def api_ambulatorio_dados():
    try:
        dados = ambulatorio.get_ambulatorio(
            request.args.get('data_ini'), request.args.get('data_fim'),
            especialidade=request.args.get('especialidade') or None,
            status=request.args.get('status') or None,
        )
        return jsonify({'ok': True, 'dados': dados})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


# ---- custos (página pública) ----

@app.route('/custos')
def custos_pagina():
    return render_template('custos.html')


@app.route('/api/custos/dados')
def api_custos_dados():
    try:
        dados = custos.get_custos(
            request.args.get('mes_ini') or None, request.args.get('mes_fim') or None,
            tipo=request.args.get('tipo') or None, item=request.args.get('item') or None,
        )
        return jsonify({'ok': True, 'dados': dados})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


# ---- lançamento de dados (área restrita): custos, produção hospitalar e SIMAQ ----
# Uma tela por tabela de banco_painel, todas com a mesma estrutura: grade do
# mês com os itens já conhecidos, linha para item novo e importação de xlsx.

LANCAMENTOS = {
    'custos': {'tabela': banco_painel.CUSTOS, 'titulo': 'Lançar Dados de Custos',
               'grupo': 'tipo', 'pagina': 'custos_pagina', 'nome_pagina': 'Custos', 'cadastro': 'item_custo'},
    'producao': {'tabela': banco_painel.PRODUCAO, 'titulo': 'Lançar Dados de Produção',
                 'grupo': 'servico', 'pagina': 'producao_hospitalar', 'nome_pagina': 'Produção Hospitalar',
                 'cadastro': 'procedimento'},
    'simaq': {'tabela': banco_painel.SIMAQ, 'titulo': 'Lançar Dados do SIMAQ',
              'grupo': 'tipo', 'pagina': 'indicadores', 'nome_pagina': 'Indicadores SIMAQ',
              'cadastro': 'indicador'},
}


@app.template_filter('num_br')
def num_br(valor, formato='decimal'):
    """Número no formato brasileiro para os campos de lançamento."""
    if valor is None or valor == '':
        return ''
    if formato == 'texto':
        return valor
    casas = {'moeda': 2, 'inteiro': 0}.get(formato, 4)
    s = f'{float(valor):,.{casas}f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    if formato == 'decimal' and ',' in s:
        s = s.rstrip('0').rstrip(',')
    return s


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
                               indisponivel=banco_painel.erro(), sql=banco_painel.sql_criar_schema())
    meses = t.meses()
    mes = request.args.get('mes')
    if not _mes_valido(mes):
        mes = meses[0]['mes'] if meses else datetime.now().strftime('%Y-%m')
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
            qtd = t.importar_planilha(caminho, g.usuario['login'])
        except Exception as e:
            flash(f'Não foi possível importar a planilha: {e}', 'erro')
            return redirect(url_for(f'admin_{chave}'))
    flash(f'Planilha importada: {qtd} lançamentos gravados.', 'ok')
    return redirect(url_for(f'admin_{chave}'))


# ---- cadastros (área restrita): tipos, itens, serviços, procedimentos e indicadores ----
# Os lançamentos só aceitam itens destas tabelas.

@app.route('/admin/cadastros')
def admin_cadastros():
    if not banco_painel.pronto():
        return render_template('admin_cadastros.html', indisponivel=banco_painel.erro(),
                               sql=banco_painel.sql_criar_schema())
    registros = {c.tabela: c.listar() for c in banco_painel.CADASTROS}
    grupos = {}
    for c in banco_painel.CADASTROS:
        grupos.setdefault(c.grupo, []).append(c)
    return render_template('admin_cadastros.html', grupos=grupos, registros=registros)


@app.route('/admin/cadastros/<tabela>/salvar', methods=['POST'])
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
                      {tab: request.form.get(f'{tab}_id', type=int) for tab, _ in cad.pais}, extras)
    flash(f'{cad.titulo}: {erro}' if erro else f'{cad.titulo}: "{nome}" salvo.', 'erro' if erro else 'ok')
    return redirect(url_for('admin_cadastros', _anchor=tabela))


@app.route('/admin/cadastros/<tabela>/<int:registro_id>/ativo', methods=['POST'])
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


@app.route('/admin/cadastros/<tabela>/<int:registro_id>/excluir', methods=['POST'])
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


for _chave in LANCAMENTOS:
    app.add_url_rule(f'/admin/{_chave}', f'admin_{_chave}', admin_lancamento,
                     defaults={'chave': _chave})
    app.add_url_rule(f'/admin/{_chave}/salvar', f'admin_{_chave}_salvar', admin_lancamento_salvar,
                     methods=['POST'], defaults={'chave': _chave})
    app.add_url_rule(f'/admin/{_chave}/importar', f'admin_{_chave}_importar', admin_lancamento_importar,
                     methods=['POST'], defaults={'chave': _chave})


@app.route('/api/hcasg/hospitalares/graficos')
def api_hcasg_hospitalares_graficos():
    clinica, enfermaria, leito = _hcasg_params_filtro()
    try:
        dados = hcasg.get_graficos_hospitalar(
            request.args.get('data_ini'), request.args.get('data_fim'), clinica, enfermaria, leito)
        return jsonify({'ok': True, 'dados': dados})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


@app.route('/api/hcasg/emergencia/indicadores')
def api_hcasg_emergencia_indicadores():
    clinica, enfermaria, leito = _hcasg_params_filtro()
    data_ini = request.args.get('data_ini')
    data_fim = request.args.get('data_fim')
    try:
        dados = get_indicadores_emergencia(data_ini, data_fim, clinica, enfermaria, leito)
        return jsonify({'ok': True, 'dados': dados})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


@app.route('/api/hcasg/emergencia/atendimentos')
def api_hcasg_emergencia_atendimentos():
    data_ini = request.args.get('data_ini')
    data_fim = request.args.get('data_fim')
    try:
        # dia_semana: 0 = segunda ... 6 = domingo (pode repetir o parâmetro)
        dias_semana = [int(d) for d in request.args.getlist('dia_semana') if d.isdigit() and int(d) <= 6]
        hora_ini = min(max(int(request.args.get('hora_ini') or 0), 0), 23)
        hora_fim = min(max(int(request.args.get('hora_fim') or 23), 0), 23)
        dados = hcasg.get_atendimentos_emergencia(data_ini, data_fim, dias_semana, hora_ini, hora_fim)
        return jsonify({'ok': True, 'dados': dados})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


@app.route('/api/hcasg/emergencia/saidas')
def api_hcasg_emergencia_saidas():
    try:
        dados = hcasg.get_saidas_emergencia(request.args.get('data_ini'), request.args.get('data_fim'))
        return jsonify({'ok': True, 'dados': dados})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


@app.route('/hcasg/hospitalares/tendencia')
def hcasg_hospitalares_tendencia():
    clinicas, enfermarias, leitos = hcasg_get_filtros()
    hoje = datetime.now().date()
    primeiro_mes_atual = hoje.replace(day=1)
    mes_fim = (primeiro_mes_atual - pd.DateOffset(months=1)).date()
    mes_ini = (primeiro_mes_atual - pd.DateOffset(months=12)).date()
    return render_template(
        'hcasg_hospitalares_tendencia.html',
        clinicas=clinicas, enfermarias=enfermarias, leitos=leitos,
        mes_ini=mes_ini.strftime('%Y-%m'), mes_fim=mes_fim.strftime('%Y-%m'),
    )


@app.route('/hcasg/emergencia/tendencia')
def hcasg_emergencia_tendencia():
    clinicas, enfermarias, leitos = hcasg_get_filtros()
    hoje = datetime.now().date()
    primeiro_mes_atual = hoje.replace(day=1)
    mes_fim = (primeiro_mes_atual - pd.DateOffset(months=1)).date()
    mes_ini = (primeiro_mes_atual - pd.DateOffset(months=12)).date()
    return render_template(
        'hcasg_emergencia_tendencia.html',
        clinicas=clinicas, enfermarias=enfermarias, leitos=leitos,
        mes_ini=mes_ini.strftime('%Y-%m'), mes_fim=mes_fim.strftime('%Y-%m'),
    )


@app.route('/hcasg/hospitalares/comparativo')
def hcasg_hospitalares_comparativo():
    clinicas, enfermarias, leitos = hcasg_get_filtros()
    hoje = datetime.now().date()
    primeiro_mes_atual = hoje.replace(day=1)
    mes_b = (primeiro_mes_atual - pd.DateOffset(months=1)).date()
    mes_a = (primeiro_mes_atual - pd.DateOffset(months=2)).date()
    return render_template(
        'hcasg_hospitalares_comparativo.html',
        clinicas=clinicas, enfermarias=enfermarias, leitos=leitos,
        mes_a=mes_a.strftime('%Y-%m'), mes_b=mes_b.strftime('%Y-%m'),
    )


@app.route('/hcasg/emergencia/comparativo')
def hcasg_emergencia_comparativo():
    clinicas, enfermarias, leitos = hcasg_get_filtros()
    hoje = datetime.now().date()
    primeiro_mes_atual = hoje.replace(day=1)
    mes_b = (primeiro_mes_atual - pd.DateOffset(months=1)).date()
    mes_a = (primeiro_mes_atual - pd.DateOffset(months=2)).date()
    return render_template(
        'hcasg_emergencia_comparativo.html',
        clinicas=clinicas, enfermarias=enfermarias, leitos=leitos,
        mes_a=mes_a.strftime('%Y-%m'), mes_b=mes_b.strftime('%Y-%m'),
    )


# ---- APIs: tendência mensal ----

@app.route('/api/hcasg/hospitalares/mensal')
def api_hcasg_hospitalares_mensal():
    clinica, enfermaria, leito = _hcasg_params_filtro()
    mes_ini = request.args.get('mes_ini') or None
    mes_fim = request.args.get('mes_fim') or None
    try:
        dados = get_mensal_hospitalar(clinica, enfermaria, leito, mes_ini, mes_fim)
        return jsonify({'ok': True, 'dados': dados})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


@app.route('/api/hcasg/emergencia/mensal')
def api_hcasg_emergencia_mensal():
    clinica, enfermaria, leito = _hcasg_params_filtro()
    mes_ini = request.args.get('mes_ini') or None
    mes_fim = request.args.get('mes_fim') or None
    try:
        dados = get_mensal_emergencia(clinica, enfermaria, leito, mes_ini, mes_fim)
        return jsonify({'ok': True, 'dados': dados})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


# ---- APIs: comparativo entre dois meses ----

@app.route('/api/hcasg/hospitalares/comparativo')
def api_hcasg_hospitalares_comparativo():
    clinica, enfermaria, leito = _hcasg_params_filtro()
    mes_a = request.args.get('mes_a')
    mes_b = request.args.get('mes_b')
    if not mes_a or not mes_b:
        return jsonify({'ok': False, 'erro': 'Informe os dois meses para comparar.'}), 400
    mes_ini = min(mes_a, mes_b)
    mes_fim = max(mes_a, mes_b)
    try:
        dados = get_mensal_hospitalar(clinica, enfermaria, leito, mes_ini, mes_fim)
        comparativo = _hcasg_montar_comparativo(dados, HCASG_CAMPOS_HOSPITALAR, mes_a, mes_b)
        if comparativo is None:
            return jsonify({'ok': False, 'erro': 'Não há dados para um dos meses selecionados.'}), 400
        return jsonify({'ok': True, **comparativo})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


@app.route('/api/hcasg/emergencia/comparativo')
def api_hcasg_emergencia_comparativo():
    clinica, enfermaria, leito = _hcasg_params_filtro()
    mes_a = request.args.get('mes_a')
    mes_b = request.args.get('mes_b')
    if not mes_a or not mes_b:
        return jsonify({'ok': False, 'erro': 'Informe os dois meses para comparar.'}), 400
    mes_ini = min(mes_a, mes_b)
    mes_fim = max(mes_a, mes_b)
    try:
        dados = get_mensal_emergencia(clinica, enfermaria, leito, mes_ini, mes_fim)
        comparativo = _hcasg_montar_comparativo(dados, HCASG_CAMPOS_EMERGENCIA, mes_a, mes_b)
        if comparativo is None:
            return jsonify({'ok': False, 'erro': 'Não há dados para um dos meses selecionados.'}), 400
        return jsonify({'ok': True, **comparativo})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


# ---------------------------------------------------------------------------
# INDICADORES LEAN — visão consolidada (hospitalares + emergência) com
# tendência mensal e auditoria dos registros. Antes era um app separado
# (porta 5200); usa as mesmas consultas de indicadores_hcasg.
# ---------------------------------------------------------------------------

def _lean_filtros_tela():
    try:
        return hcasg_get_filtros()
    except Exception:
        return [], [], []


@app.route('/lean/')
def lean_indicadores():
    clinicas, enfermarias, leitos = _lean_filtros_tela()
    hoje = datetime.now().date()
    return render_template(
        'lean_indicadores.html',
        clinicas=clinicas, enfermarias=enfermarias, leitos=leitos,
        data_ini=hoje.replace(day=1).strftime('%Y-%m-%d'), data_fim=hoje.strftime('%Y-%m-%d'),
    )


@app.route('/lean/tendencias')
def lean_tendencias():
    clinicas, enfermarias, leitos = _lean_filtros_tela()
    primeiro_mes_atual = datetime.now().date().replace(day=1)
    mes_fim = (primeiro_mes_atual - pd.DateOffset(months=1)).date()
    mes_ini = (primeiro_mes_atual - pd.DateOffset(months=12)).date()
    return render_template(
        'lean_tendencias.html',
        clinicas=clinicas, enfermarias=enfermarias, leitos=leitos,
        mes_ini=mes_ini.strftime('%Y-%m'), mes_fim=mes_fim.strftime('%Y-%m'),
    )


@app.route('/lean/auditoria')
def lean_auditoria():
    hoje = datetime.now().date()
    return render_template(
        'lean_auditoria.html',
        data_ini=hoje.replace(day=1).strftime('%Y-%m-%d'), data_fim=hoje.strftime('%Y-%m-%d'),
    )


@app.route('/lean/api/indicadores')
def api_lean_indicadores():
    clinica, enfermaria, leito = _hcasg_params_filtro()
    data_ini = request.args.get('data_ini')
    data_fim = request.args.get('data_fim')
    try:
        dados = hcasg.get_indicadores(data_ini, data_fim, clinica, enfermaria, leito)
        return jsonify({'ok': True, 'dados': dados})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


@app.route('/lean/api/indicadores_mensais')
def api_lean_indicadores_mensais():
    clinica, enfermaria, leito = _hcasg_params_filtro()
    mes_ini = request.args.get('mes_ini') or None
    mes_fim = request.args.get('mes_fim') or None
    try:
        dados = hcasg.get_indicadores_mensais(clinica, enfermaria, leito, mes_ini, mes_fim)
        return jsonify({'ok': True, 'dados': dados})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


LEAN_AUDITORIAS = {
    'los_com': hcasg.get_auditoria_los_com,
    'los_sem': hcasg.get_auditoria_los_sem,
    'porta_medico': hcasg.get_auditoria_porta_medico,
    'internacoes_ps': hcasg.get_auditoria_internacoes_ps,
    'tmph': hcasg.get_auditoria_tmph,
    'tmp_ps': hcasg.get_auditoria_tmp_ps,
}


@app.route('/lean/api/auditoria_<indicador>')
def api_lean_auditoria(indicador):
    consulta = LEAN_AUDITORIAS.get(indicador)
    if consulta is None:
        return jsonify({'ok': False, 'erro': f'Indicador de auditoria desconhecido: {indicador}'}), 404
    try:
        dados = consulta(request.args.get('data_ini'), request.args.get('data_fim'))
        return jsonify({'ok': True, 'dados': dados})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


if __name__ == '__main__':
    app.template_folder = os.path.join(BASE_DIR, 'templates')
    app.static_folder = os.path.join(BASE_DIR, 'static')
    print(f"Pasta de templates: {app.template_folder}")
    print(f"Pasta de arquivos estáticos: {app.static_folder}")
    # Diagnóstico: liste os arquivos do diretório de templates
    print("[DEBUG] Arquivos na pasta de templates:", os.listdir(app.template_folder))
    app.run(debug=True, host='0.0.0.0', port=5500)