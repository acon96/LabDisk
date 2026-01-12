set -e
docker build . --platform=linux/amd64 -t docker-registry.home/lab-disk:latest
docker push docker-registry.home/lab-disk:latest
kubectl -n kube-system get pods | grep lab-disk | awk '{print $1}' | xargs kubectl -n kube-system delete pod