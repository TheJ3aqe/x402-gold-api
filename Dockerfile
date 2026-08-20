# Build from the income/ directory, NOT from x402-gold-api/ alone:
#   docker build -f x402-gold-api/Dockerfile -t x402-gold-api income/
#
# Why: x402api/cot_source.py reuses income/apify-cot-analytics/src/ as a
# sibling package at a fixed relative path (../../apify-cot-analytics/src
# from x402api/), so both directories must be in the build context together.
# See x402api/cot_source.py's module docstring for the reuse mechanism.

FROM python:3.12-slim

WORKDIR /app
COPY apify-cot-analytics/src ./apify-cot-analytics/src
COPY x402-gold-api ./x402-gold-api
WORKDIR /app/x402-gold-api

RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONUNBUFFERED=1
EXPOSE 8402

# $PORT is how Render (and most PaaS) tell a container which port to bind;
# defaults to 8402 for Fly.io / plain `docker run` where nothing sets it.
CMD ["sh", "-c", "python -m x402api --host 0.0.0.0 --port ${PORT:-8402}"]
