FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY App.py .

EXPOSE 5000

CMD ["python", "App.py"]
