FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TRUST_PROXY=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py config.py ./
COPY public ./public
RUN useradd -m app
USER app
EXPOSE 8000
CMD ["sh", "-c", "python main.py"]
