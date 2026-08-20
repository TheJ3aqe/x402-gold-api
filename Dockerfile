# Build from THIS directory (repo root) -- standard for Render/Fly.io,
# which build from the repo root by default:
#   docker build -t x402-gold-api .
#
# x402api/cot_source.py loads apify-cot-analytics/src/ (vendored into this
# repo, see that module's docstring for why -- this used to be a sibling of
# income/x402-gold-api/ in the private monorepo; now it ships as a vendored
# copy inside this public repo so the build has no dependency outside it).

FROM python:3.12-slim

WORKDIR /app

COPY apify-cot-analytics/src ./apify-cot-analytics/src
COPY . .

RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONUNBUFFERED=1
EXPOSE 8402

# $PORT is how Render (and most PaaS) tell a container which port to bind;
# defaults to 8402 for Fly.io / plain `docker run` where nothing sets it.
CMD ["sh", "-c", "python -m x402api --host 0.0.0.0 --port ${PORT:-8402}"]
