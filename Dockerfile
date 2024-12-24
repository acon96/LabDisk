FROM python:3.13-slim

WORKDIR /app

RUN apt update && apt install -y nfs-kernel-server rpcbind lvm2 mdadm xfsprogs
ADD requirements.txt .
RUN pip3 install -r requirements.txt

ADD *.py /app/

# kopf run --verbose /app/handler.py
ENTRYPOINT ["python3", "-m"]
CMD ["kopf", "run", "--verbose", "/app/handler.py", "--all-namespaces"]