#!/bin/bash

echo "Confirm audio is actually being produced inside the container"

docker exec -it <android_container> ls -la /data/android_output/
