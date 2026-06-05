#!/bin/bash

LOCKFILE="/tmp/netbox-sync.lock"
LOGFILE="/var/log/netbox-sync/netbox-sync.log"
# Caminho do seu projeto
PROJECT_DIR="/opt/netbox-sync"

exec 9>$LOCKFILE

if ! flock -n 9; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - Já está rodando, saindo..." >> $LOGFILE
    exit 0
fi

# IMPORTANTE: Carrega as variáveis antes de iniciar
cd $PROJECT_DIR
#export $(grep -v '^#' netbox-sync.env | xargs)

echo "$(date '+%Y-%m-%d %H:%M:%S') - Iniciando sincronização..." >> $LOGFILE

# Executa o Python do VENV
$PROJECT_DIR/venv/bin/python $PROJECT_DIR/main.py >> $LOGFILE 2>&1

echo "$(date '+%Y-%m-%d %H:%M:%S') - Finalizado." >> $LOGFILE
