FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY config.py settings.py auth.py notify.py vinted_api.py store.py worker.py app.py reposter.py healthcheck.py ./
COPY templates ./templates
# readable for whatever PUID the container is told to run as
RUN chmod -R a+rX /app
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python", "healthcheck.py"]
CMD ["python", "-u", "reposter.py"]
