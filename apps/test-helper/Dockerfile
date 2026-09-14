# Build the base first: docker build -t a2a-pack-base:latest -f sdk/a2a-pack/Dockerfile sdk/a2a-pack
ARG BASE_IMAGE=a2a-pack-base:latest
FROM ${BASE_IMAGE}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV A2A_ENTRYPOINT=agent:TestHelperAgent
ENV PORT=8000
EXPOSE 8000

CMD a2a run --entrypoint "$A2A_ENTRYPOINT" --host 0.0.0.0 --port 8000
