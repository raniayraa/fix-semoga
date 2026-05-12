./experiment_runner.py \
      --ports 1024,1024-1025,1024-1026,1024-1027,1024-1028,1024-1029,1024-1030,1024-1031,1024-1032,1024-1033 \
      --traffic 41,15,15_41 \
      --forwarder xdp \
      --inventory ansible_automasi/inventory.ini \
      --ansible-dir ansible_automasi \
      2>&1 | tee "experiments_$(date +%Y%m%d_%H%M%S).log"