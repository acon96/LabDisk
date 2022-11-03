FROM python:3.7-slim

WORKDIR /app

RUN apt update && apt install -y nfs-kernel-server rpcbind
ADD requirements.txt .
RUN pip3 install -r requirements.txt

ADD *.py .

# kopf run --verbose /app/handler.py
ENTRYPOINT ["python3", "-m"]
CMD ["kopf", "run", "--verbose", "/app/handler.py"]