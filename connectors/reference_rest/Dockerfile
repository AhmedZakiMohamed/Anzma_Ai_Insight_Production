FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/reference-rest
COPY connectors/sdk ./connectors/sdk
COPY connectors/reference_rest ./connectors/reference_rest
RUN mkdir -p /var/lib/anzma-connector && chown -R 10001:10001 /var/lib/anzma-connector
ENV ANZMA_CONNECTOR_STATE_PATH=/var/lib/anzma-connector/reference-rest.sqlite3
USER 10001:10001
EXPOSE 18111
ENTRYPOINT ["python", "-m", "connectors.reference_rest.server"]
