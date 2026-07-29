FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONPATH=/app/src
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && pip check
COPY src ./src

EXPOSE 5056
ENV MAGPIE_REGISTRY_HOST=0.0.0.0
ENTRYPOINT ["python", "-m", "magpie_voice_registry.server"]
