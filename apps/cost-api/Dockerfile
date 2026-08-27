# Build from the REPO ROOT (not apps/cost-api/) -- needs packages/cost-engine
# as a sibling directory to install as an editable dependency.
#   docker build -f apps/cost-api/Dockerfile -t cost-api .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY packages/cost-engine ./packages/cost-engine
COPY apps/cost-api/requirements.txt ./apps/cost-api/requirements.txt
RUN pip install --no-cache-dir -e ./packages/cost-engine \
 && pip install --no-cache-dir -r ./apps/cost-api/requirements.txt

COPY apps/cost-api/app ./apps/cost-api/app
COPY infra/migrations ./infra/migrations

RUN chown -R app:app /app
USER app

WORKDIR /app/apps/cost-api/app
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
