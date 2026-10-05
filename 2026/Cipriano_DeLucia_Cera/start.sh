#!/bin/bash

# 0. Attiva gli appunti condivisi di VirtualBox
VBoxClient --clipboard 2>/dev/null

# Percorso della cartella del progetto
PROJECT_DIR="$HOME/Desktop/ncis-project-work-2026-main"

# Blocco di comandi per caricare l'ambiente pyenv e l'ambiente virtuale ryu38-env
ENV_SETUP="cd $PROJECT_DIR && export PYENV_ROOT=\"\$HOME/.pyenv\" && export PATH=\"\$PYENV_ROOT/bin:\$PATH\" && eval \"\$(pyenv init -)\" && eval \"\$(pyenv virtualenv-init -)\" && pyenv activate ryu38-env"

# Comandi specifici per ciascun terminale
CMD_CONTROLLER="$ENV_SETUP && echo '=== [1/3] AVVIO CONTROLLER RYU ===' && ryu-manager controller.py --ofp-tcp-listen-port 6633; exec bash"
CMD_DASHBOARD="$ENV_SETUP && echo '=== [2/3] AVVIO DASHBOARD FLASK ===' && python3 dashboard.py; exec bash"
CMD_TOPOLOGY="$ENV_SETUP && echo '=== [3/3] AVVIO TOPOLOGIA MININET ===' && sudo \$(which python3) topology.py; exec bash"

echo "Avvio automatizzato del progetto SDN in corso..."

# 1. Avvia Terminale 1 - Controller Ryu
gnome-terminal --title="1. Controller Ryu" -- bash -c "$CMD_CONTROLLER"
sleep 2

# 2. Avvia Terminale 2 - Dashboard Flask
gnome-terminal --title="2. Dashboard Web" -- bash -c "$CMD_DASHBOARD"
sleep 1

# Apri il browser predefinito sulla Dashboard
if command -v xdg-open &> /dev/null; then
    xdg-open "http://127.0.0.1:5050" &> /dev/null &
fi
sleep 1

# 3. Avvia Terminale 3 - Topologia Mininet (richiederà la password di sudo)
gnome-terminal --title="3. Topologia Mininet" -- bash -c "$CMD_TOPOLOGY"

echo "Tutti e tre i terminali sono stati avviati con successo!"
