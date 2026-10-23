# Painel de Indicadores — Hospital de Messejana

Painel web (Flask) com os indicadores do hospital: produção hospitalar, SIMAQ,
LEAN, exames, indicadores hospitalares e da emergência, ambulatório e custos.
Os dados vêm do banco do sistema hospitalar (PostgreSQL, só leitura), de
planilhas da pasta `dados/` e dos lançamentos feitos na área restrita.

## Funcionalidades

**Painéis públicos** (menu inicial, sem login):

| Painel | Rota | Fonte |
|---|---|---|
| Produção Hospitalar | `/producao_hospitalar` | schema `painel` (ou planilha) |
| Indicadores SIMAQ | `/indicadores` | schema `painel` (ou planilha) |
| Indicadores LEAN | `/lean/` | sistema hospitalar |
| Exames | `/exames_por_clinica`, `/graficos_mensais` | sistema hospitalar |
| Indicadores Hospitalares | `/hcasg/hospitalares` (+ tendência e comparativo) | sistema hospitalar |
| Indicadores da Emergência | `/hcasg/emergencia` (+ tendência e comparativo) | sistema hospitalar |
| Ambulatório | `/ambulatorio` | sistema hospitalar |
| Custos | `/custos` | schema `painel` |

**Área restrita** (`/admin`, com login):

- Lançamento de custos por unidade, de produção hospitalar e do SIMAQ.
- Importação da planilha mensal de custos (aba GERAL), com pré-visualização antes de gravar.
- Cadastros: unidades, grupos de unidades e itens.
- Usuários e perfis de acesso: cada perfil libera só algumas telas.
- Configuração do Banco (só administrador):
  - dados de conexão, guardados criptografados;
  - escolha das **páginas visíveis** no menu inicial.

Segurança:
- proteção CSRF em todos os formulários;
- bloqueio após várias tentativas de login erradas;
- troca de senha obrigatória no primeiro acesso;
- logout automático após **15 minutos sem uso**.

## Estrutura

```
painel.py              app Flask e rotas dos painéis
area_restrita.py       login, permissões e telas de /admin
indicadores_hcasg.py   consultas dos indicadores hospitalares e da emergência
ambulatorio.py         consultas do ambulatório
custos.py              painel de custos
banco_painel.py        schema "painel" (lançamentos) e migração das planilhas
lancamento_custos.py   lançamento de custos por unidade
importacao_custos.py   importação da planilha mensal de custos
config_banco.py        configuração do PostgreSQL (criptografada)
paginas_visiveis.py    painéis visíveis no menu inicial
usuarios.py            usuários e perfis (SQLite)
templates/             páginas (Jinja2); _layout.html é o layout comum
static/                CSS único (css/painel.css), JS e imagens
dados/                 planilhas de origem
sql/                   SQL de apoio (criação do schema painel)
tests/                 testes (pytest)
```

## Banco de dados

O painel usa **um** PostgreSQL com dois schemas:

- **Sistema hospitalar** (padrão `public`): tabelas `t_internacao`, `t_exame` etc. O painel só lê.
- **Dados do painel** (padrão `painel`): lançamentos de custos, produção e SIMAQ.
  - O painel cria as tabelas e migra as planilhas de `dados/` sozinho.
  - Se o usuário do banco não puder criar o schema, rode antes [sql/painel_schema.sql](sql/painel_schema.sql) como dono do banco.

A conexão é informada em **Administração > Configuração do Banco**: o botão
"Testar conexão" confere os dois schemas. Também dá para usar as variáveis
`DB_*` (veja abaixo). Quando os dois existem, a página tem prioridade.

## Variáveis de ambiente

Todas são opcionais.

| Variável | Para quê |
|---|---|
| `ADMIN_USER` / `ADMIN_PASS` | Administrador criado no primeiro início. Sem senha, uma aleatória sai no log. |
| `FLASK_SECRET_KEY` | Chave das sessões. Sem ela, é gerada em `.secret_key`. |
| `PAINEL_CHAVE_CONFIG` | Chave que criptografa a configuração do banco. Sem ela, é gerada em `.chave_config`. |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASS`, `DB_SCHEMA`, `DB_SCHEMA_PAINEL` | Conexão com o PostgreSQL. |
| `PAINEL_PASTA_LOCAL` | Pasta dos arquivos locais. Padrão: a pasta do projeto; no Docker: o volume `/app/local`. |
| `PAINEL_DEBUG=1` | Modo debug, só em desenvolvimento (escuta só em 127.0.0.1). |

**Arquivos locais.** Ficam fora do git e devem entrar no backup:

| Arquivo | Conteúdo |
|---|---|
| `.secret_key` | chave das sessões |
| `.chave_config` | chave de criptografia |
| `config_banco.enc` | conexão com o banco |
| `config_paginas.json` | páginas visíveis |
| `painel_usuarios.db` | usuários e perfis |
| `painel_dados.db` | custos antigos, usados só na migração |

> `config_banco.enc` só abre com a mesma chave (`.chave_config` ou
> `PAINEL_CHAVE_CONFIG`). Copie os dois juntos.

## Executar com Docker Compose

Requisitos: Docker com Compose v2. No Windows, o Docker Desktop precisa do WSL 2.

```bash
cp .env.example .env        # ajuste ADMIN_PASS, DB_* etc.
docker compose up -d --build
docker compose logs -f painel
```

O painel abre em `http://<servidor>:5500`. A porta muda em `PAINEL_PORTA`, no `.env`.

Como o contêiner funciona:
- Roda com **gunicorn** (1 processo e 8 threads), como usuário sem privilégios, no fuso `America/Fortaleza`.
- Os arquivos locais ficam no volume `painel_local`, montado em `/app/local`. Eles sobrevivem a atualizações da imagem.
- Se o PostgreSQL roda na própria máquina do Docker, use `DB_HOST=host.docker.internal`.

**Levar a configuração de uma instalação existente para o volume:**

```bash
docker compose up -d
for f in .secret_key .chave_config config_banco.enc config_paginas.json painel_usuarios.db painel_dados.db; do
  [ -f "$f" ] && docker compose cp "$f" painel:/app/local/
done
docker compose exec -u root painel chown -R painel:painel /app/local
docker compose restart painel
```

**Atualizar:**

```bash
git pull
docker compose up -d --build
```

**Backup do volume:**

```bash
docker run --rm -v projeto_painel_painel_local:/dados -v "$PWD":/backup alpine \
  tar czf /backup/painel_local.tgz -C /dados .
```

O nome do volume segue o nome da pasta do projeto: confira com `docker volume ls`.

## Executar localmente (desenvolvimento)

Requisitos: Python 3.11+ (testado com 3.13).

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows (Linux: source .venv/bin/activate)
pip install -r requirements-dev.txt
python painel.py                  # http://localhost:5500
```

No VS Code, a configuração "Painel (local, com debug)" do `.vscode/launch.json`
roda com `PAINEL_DEBUG=1`.

No primeiro início, o administrador `admin` é criado:
- a senha vem de `ADMIN_PASS` ou sai no terminal;
- a troca de senha é obrigatória no primeiro acesso.

## Testes

```bash
python -m pytest tests
```

Os testes não precisam de PostgreSQL: usam um SQLite de usuários temporário.
