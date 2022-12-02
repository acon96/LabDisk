#!/bin/bash

docker build . -t acon96/lab-disk:latest

if [[ ! -z $1 ]]; then
  docker tag acon96/lab-disk:latest acon96/lab-disk:$1
fi

read -r -p "Push? [y/N] " response
if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
    docker push acon96/lab-disk:latest

    if [[ ! -z $1 ]]; then
        docker push acon96/lab-disk:$1
    fi
fi
