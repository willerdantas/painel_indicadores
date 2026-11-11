"""
seguranca.py

Proteções gerais do painel; painel.py chama registrar(app) uma vez.
- Cabeçalhos de segurança em toda resposta (CSP, nosniff, referrer...). A
  área restrita e o login também não podem ser abertos dentro de outro site
  (iframe); os painéis públicos podem, para continuarem embutíveis em portais.
- HTTPS opcional: PAINEL_HTTPS=1 quando o painel fica atrás de um proxy
  reverso que termina o TLS (nginx, IIS...). Liga o ProxyFix (IP e protocolo
  reais vêm do X-Forwarded-*), redireciona http -> https, envia HSTS e marca
  o cookie de sessão como Secure. Sem proxy, deixe desligado: o ProxyFix
  confiaria em cabeçalhos que qualquer cliente pode forjar.
- Limite de tamanho das requisições (planilhas e logos enviadas).
- Parâmetros de período (data, mês, ano, hora, dia da semana) conferidos antes
  das rotas: formato inválido responde 400, sem chegar ao banco.
- erro_interno(): erro 500 sem detalhes técnicos para o navegador; o detalhe
  (com o traceback) vai para o log do servidor.
"""

import os
import re
import traceback
from datetime import datetime

from flask import jsonify, redirect, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

HTTPS = os.environ.get('PAINEL_HTTPS') == '1'

TAMANHO_MAXIMO_REQUISICAO = 32 * 1024 * 1024  # maior planilha do SIAFE fica bem abaixo disso
TAMANHO_MAXIMO_PARAMETRO = 500

MSG_ERRO_INTERNO = 'Não foi possível carregar os dados. Tente de novo; se continuar, avise o administrador do painel.'

# scripts e estilos inline continuam liberados (todas as páginas usam); de
# fora, só os CDNs dos gráficos e das exportações
CSP = '; '.join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.plot.ly https://cdnjs.cloudflare.com",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])

# o Plotly 1.x (CDN) monta funções com eval ao carregar: só as páginas que o
# usam liberam 'unsafe-eval'
PAGINAS_PLOTLY = ('/producao_hospitalar', '/indicadores', '/faturamento', '/graficos_mensais')
CSP_PLOTLY = CSP.replace("'unsafe-inline'", "'unsafe-inline' 'unsafe-eval'", 1)

_DATA = '%Y-%m-%d'
_MES = '%Y-%m'
_FORMATOS = {
    'data_ini': _DATA, 'data_fim': _DATA,
    'mes': _MES, 'mes_ini': _MES, 'mes_fim': _MES, 'mes_a': _MES, 'mes_b': _MES,
    'ano_a': re.compile(r'\d{4}'), 'ano_b': re.compile(r'\d{4}'),
    'hora_ini': re.compile(r'\d{1,2}'), 'hora_fim': re.compile(r'\d{1,2}'),
    'dia_semana': re.compile(r'[0-6]'),
}


def _protegida(caminho):
    """Área restrita e login: sem iframe nem cache."""
    return caminho in ('/login', '/admin') or caminho.startswith('/admin/')


def _eh_requisicao_de_dados():
    return '/api/' in request.path or request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _valor_valido(formato, valor):
    if isinstance(formato, str):
        try:
            datetime.strptime(valor, formato)
            return True
        except ValueError:
            return False
    return formato.fullmatch(valor) is not None


def _parametro_invalido():
    """Nome do primeiro parâmetro da URL fora do formato (vazio é aceito: as
    rotas usam o padrão), ou None."""
    for nome, valores in request.args.lists():
        for valor in valores:
            if len(valor) > TAMANHO_MAXIMO_PARAMETRO:
                return nome
            formato = _FORMATOS.get(nome)
            if valor and formato is not None and not _valor_valido(formato, valor):
                return nome
    return None


def _responder_erro(mensagem, status):
    if _eh_requisicao_de_dados():
        return jsonify({'ok': False, 'erro': mensagem}), status
    return render_template('error.html', message=mensagem), status


def _registrar_no_log():
    print(f'[painel] erro em {request.method} {request.full_path}:\n{traceback.format_exc()}', flush=True)


def erro_interno(mensagem=MSG_ERRO_INTERNO):
    """Resposta JSON de erro 500 sem detalhes técnicos; o detalhe vai para o
    log. Chamar dentro do except."""
    _registrar_no_log()
    return jsonify({'ok': False, 'erro': mensagem}), 500


def erro_interno_pagina(mensagem=MSG_ERRO_INTERNO, status=500):
    """Como erro_interno, para rotas que devolvem página."""
    _registrar_no_log()
    return render_template('error.html', message=mensagem), status


def forcar_https():
    if HTTPS and not request.is_secure and request.path != '/saude':
        return redirect(request.url.replace('http://', 'https://', 1), code=301)
    return None


def conferir_parametros():
    nome = _parametro_invalido()
    if nome is not None:
        return _responder_erro(f'Parâmetro inválido: {nome}.', 400)
    return None


def cabecalhos(resposta):
    h = resposta.headers
    protegida = _protegida(request.path)
    csp = CSP_PLOTLY if request.path in PAGINAS_PLOTLY else CSP
    h.setdefault('Content-Security-Policy', csp + ("; frame-ancestors 'none'" if protegida else ''))
    h.setdefault('X-Content-Type-Options', 'nosniff')
    h.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    h.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=(), usb=()')
    h.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
    h.setdefault('X-Robots-Tag', 'noindex, nofollow')
    if protegida:
        h['X-Frame-Options'] = 'DENY'
        h['Cache-Control'] = 'no-store'
    if HTTPS and request.is_secure:
        h.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return resposta


def requisicao_grande(e):
    return _responder_erro('Arquivo grande demais (máximo de '
                           f'{TAMANHO_MAXIMO_REQUISICAO // (1024 * 1024)} MB).', 413)


def saude():
    """Verificação de vida do container (HEALTHCHECK do Dockerfile): não
    consulta o banco nem exige HTTPS."""
    return 'ok', 200, {'Content-Type': 'text/plain; charset=utf-8'}


def robots():
    return 'User-agent: *\nDisallow: /\n', 200, {'Content-Type': 'text/plain; charset=utf-8'}


def registrar(app):
    app.config.update(
        MAX_CONTENT_LENGTH=TAMANHO_MAXIMO_REQUISICAO,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=HTTPS,
    )
    if HTTPS:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    # registrados antes do controle de acesso (area_restrita.registrar)
    app.before_request(forcar_https)
    app.before_request(conferir_parametros)
    app.after_request(cabecalhos)
    app.register_error_handler(413, requisicao_grande)
    app.add_url_rule('/saude', 'saude', saude)
    app.add_url_rule('/robots.txt', 'robots', robots)
