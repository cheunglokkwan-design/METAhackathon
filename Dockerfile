# Free https hosting on Hugging Face Spaces (Docker SDK) or Render.
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server server
COPY web web
# Hugging Face Spaces expects port 7860; Render sets $PORT.
ENV PORT=7860
EXPOSE 7860
CMD uvicorn server.app:app --host 0.0.0.0 --port ${PORT}
