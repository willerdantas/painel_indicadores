"""
logos.py

Logos do painel, trocáveis em /admin/banco (só administrador):

  cabecalho   logo do topo de todas as páginas
  carregando  logo da tela de carregamento

Os templates usam as variáveis logo_cabecalho e logo_carregando (endereço
da imagem). A logo enviada fica em <PAINEL_PASTA_LOCAL>/logos/ e é servida em
/logo/<tipo>; sem logo enviada vale a imagem padrão de static/.
"""

import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# arquivos locais (chaves, configuração, SQLite): PAINEL_PASTA_LOCAL ou a pasta do projeto
PASTA_LOCAL = os.environ.get('PAINEL_PASTA_LOCAL') or BASE_DIR
PASTA = os.path.join(PASTA_LOCAL, 'logos')

# tipo: (nome na tela, imagem padrão em static/)
LOGOS = {
    'cabecalho': ('Logo do cabeçalho', 'LOGO HM E SESA - NOVA (1).png'),
    'carregando': ('Logo da tela de carregamento', 'LOGO HM - NOVA (1).png'),
}

TAMANHO_MAXIMO = 2 * 1024 * 1024

# formatos aceitos (extensão: tipo MIME), conferidos pelo conteúdo e não pela
# extensão. SVG fica de fora: pode carregar script e seria servido pelo próprio painel.
MIMETYPES = {'png': 'image/png', 'jpg': 'image/jpeg', 'webp': 'image/webp'}

_lock = threading.Lock()


class LogoInvalida(ValueError):
    pass


def _formato(conteudo):
    if conteudo.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if conteudo.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if conteudo[:4] == b'RIFF' and conteudo[8:12] == b'WEBP':
        return 'webp'
    return None


def arquivo_personalizado(tipo):
    """Caminho da logo enviada para o tipo, ou None (vale a padrão)."""
    for extensao in MIMETYPES:
        caminho = os.path.join(PASTA, f'{tipo}.{extensao}')
        if os.path.exists(caminho):
            return caminho
    return None


def salvar(tipo, conteudo):
    if tipo not in LOGOS:
        raise LogoInvalida('Logo desconhecida.')
    if not conteudo:
        raise LogoInvalida('Escolha uma imagem.')
    if len(conteudo) > TAMANHO_MAXIMO:
        raise LogoInvalida('A imagem deve ter no máximo 2 MB.')
    extensao = _formato(conteudo)
    if extensao is None:
        raise LogoInvalida('Formato não aceito: envie uma imagem PNG, JPG ou WebP.')
    with _lock:
        os.makedirs(PASTA, exist_ok=True)
        tmp = os.path.join(PASTA, f'{tipo}.{extensao}.tmp')
        with open(tmp, 'wb') as f:
            f.write(conteudo)
        _remover(tipo)
        os.replace(tmp, os.path.join(PASTA, f'{tipo}.{extensao}'))


def restaurar(tipo):
    """Volta para a logo padrão."""
    with _lock:
        _remover(tipo)


def _remover(tipo):
    for extensao in MIMETYPES:
        try:
            os.remove(os.path.join(PASTA, f'{tipo}.{extensao}'))
        except FileNotFoundError:
            pass


def enderecos(url_static, url_logo):
    """{tipo: endereço da imagem} para os templates. A versão (data do
    arquivo) no endereço faz o navegador buscar a logo nova logo após a troca."""
    saida = {}
    for tipo, (_nome, padrao) in LOGOS.items():
        caminho = arquivo_personalizado(tipo)
        if caminho:
            saida[tipo] = url_logo(tipo, v=int(os.path.getmtime(caminho)))
        else:
            saida[tipo] = url_static(padrao)
    return saida


def listar(enderecos_atuais):
    """Logos para a tela de configuração."""
    return [{'tipo': tipo, 'nome': nome, 'url': enderecos_atuais[tipo],
             'personalizada': arquivo_personalizado(tipo) is not None}
            for tipo, (nome, _padrao) in LOGOS.items()]
