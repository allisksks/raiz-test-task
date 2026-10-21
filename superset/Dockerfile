FROM apache/superset:4.1.2

USER root
# Драйвер PostgreSQL: для метаданных Superset и подключения к хранилищу
RUN pip install --no-cache-dir "psycopg2-binary==2.9.10"
USER superset

COPY --chown=superset superset_config.py /app/pythonpath/superset_config.py
COPY --chown=superset --chmod=755 bootstrap.sh /app/bootstrap.sh
