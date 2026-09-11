# GRS Manager: ponte rotctld (4533) + painel do operador (5590).
#
# O Dockerfile mais simples da estação, e isso é resultado do desenho: sem git
# (nenhuma dependência vem de git URL), sem gcc e sem libpq-dev (não há driver
# de banco para compilar). Este é o único serviço que não conhece o Postgres.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e .

# --host/--status-host precisam ser 0.0.0.0: os defaults são 127.0.0.1, que
# dentro do container não aceitam conexão vinda de fora dele.
CMD ["python", "-m", "grs_manager.main", \
     "--host=0.0.0.0", "--port=4533", \
     "--status-host=0.0.0.0", "--status-port=5590"]
