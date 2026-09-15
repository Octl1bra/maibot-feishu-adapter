FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY maibot_feishu ./maibot_feishu
EXPOSE 8765
USER 65532:65532
CMD ["python", "-m", "maibot_feishu"]
