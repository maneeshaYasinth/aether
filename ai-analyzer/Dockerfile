FROM python:3.14-slim

# Don't write .pyc files; flush logs straight to `kubectl logs`.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt changes,
# so code-only changes rebuild in seconds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector/ collector/
COPY analyzer/ analyzer/
COPY proposer/ proposer/
COPY run.py .

# Run as an unprivileged user; the job only needs to make HTTPS calls.
RUN useradd --uid 10001 --no-create-home analyzer
USER 10001

ENTRYPOINT ["python", "run.py"]
