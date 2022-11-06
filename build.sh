docker build . -t docker-registry.home/lab-disk:latest
docker push docker-registry.home/lab-disk:latest
# kubectl -n kube-system delete pod -l app=storage