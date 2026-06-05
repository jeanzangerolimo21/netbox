#!/bin/bash
## Navega até o diretório do projeto
cd /opt/netbox-sync
#
## Carrega as variáveis de ambiente do seu arquivo .env
export $(grep -v '^#' netbox-sync.env | xargs)
#
## Executa o script usando o Python do seu ambiente virtual (venv)
## Isso garante que todas as bibliotecas (proxmoxer, pynetbox) sejam encontradas
/opt/netbox-sync/venv/bin/python3 main.py >> /var/log/netbox-sync.log 2>&1
