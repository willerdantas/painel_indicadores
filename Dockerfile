# Painel de Indicadores - imagem de produção (gunicorn).
# Uso: docker compose up -d --build (ver README.md)
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Fortaleza \
    PAINEL_PASTA_LOCAL=/app/local

# fuso horário (o "último mês fechado" e os lançamentos dependem da data local)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# usuário sem privilégios; /app/local guarda chaves, configuração e o SQLite de usuários (volume)
RUN useradd --create-home --uid 1000 painel \
    && mkdir -p /app/local \
    && chown -R painel:painel /app/local
USER painel

EXPOSE 5500

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5500/login', timeout=4)"

# 1 processo com várias threads: o bloqueio de tentativas de login e o estado
# da conexão com o banco ficam em memória e precisam ser únicos.
CMD ["gunicorn", "--bind", "0.0.0.0:5500", "--workers", "1", "--threads", "8", \
     "--timeout", "180", "--access-logfile", "-", "painel:app"]
