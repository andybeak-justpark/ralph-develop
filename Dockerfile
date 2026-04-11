FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY lib/ lib/
COPY prompts/ prompts/
COPY run.py .

ENTRYPOINT ["python3", "run.py"]
