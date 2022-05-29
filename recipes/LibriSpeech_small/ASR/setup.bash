. ~/myenv/bin/activate
unset XDG_RUNTIME_DIR
export NODEIP=$(hostname -i)
export NODEPORT=$(( $RANDOM + 1024 ))
echo $NODEIP:$NODEPORT

