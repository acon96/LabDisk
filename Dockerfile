FROM python:3.7-slim

WORKDIR /app

ADD requirements.txt .
RUN pip3 install -r requirements.txt

ADD *.py .

# kopf run --verbose /app/handler.py
ENTRYPOINT ["python3", "-m"]
CMD ["kopf", "run", "--verbose", "/app/handler.py"]