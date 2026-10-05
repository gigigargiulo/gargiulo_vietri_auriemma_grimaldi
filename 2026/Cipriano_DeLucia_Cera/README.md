<div align="center">

# 🌐 SDN Service Slicing & Behavioral DoS Mitigation

### *Infrastruttura di Rete Programmabile OpenFlow 1.3 con Slicing Reattivo dei Servizi, Rilevamento Comportamentale degli Attacchi DoS e Dashboard di Monitoraggio in Tempo Reale*

[![Python 3.8](https://img.shields.io/badge/Python-3.8.18-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Ryu 4.34](https://img.shields.io/badge/SDN_Controller-Ryu_v4.34-00599C?style=for-the-badge&logo=openvswitch&logoColor=white)](https://ryu-sdn.org/)
[![OpenFlow 1.3](https://img.shields.io/badge/Protocol-OpenFlow_1.3-E34F26?style=for-the-badge&logo=ethernet&logoColor=white)](https://www.opennetworking.org/)
[![Mininet](https://img.shields.io/badge/Emulation-Mininet_v2.3-22C55E?style=for-the-badge&logo=linux&logoColor=white)](http://mininet.org/)
[![Flask 2.2](https://img.shields.io/badge/Dashboard-Flask_v2.2.5-000000?style=for-the-badge&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![UniNa](https://img.shields.io/badge/University-UniNa_Federico_II-B91C1C?style=for-the-badge)](https://www.unina.it/)


---

</div>

## 📖 Panoramica del Progetto

Questo progetto realizza un'infrastruttura di rete **Software-Defined Networking (SDN)** orientata alla sicurezza e alla qualità del servizio (**QoS**), sviluppata per il corso di *Network and Cloud Infrastructure Security (NCIS)* presso l'**Università degli Studi di Napoli Federico II**.

Attraverso la separazione tra piano di controllo (*Ryu Controller*) e piano dati (*Mininet / Open vSwitch*), il sistema risolve due problematiche critiche delle reti moderne:
1. **Service Slicing Reattivo**: Suddivisione logica di un'unica infrastruttura fisica in **fette di rete (slices) isolate**, assegnando percorsi dedicati e bande garantite al traffico **Video** (UDP 9999) e **Web** (TCP 5001).
2. **Mitigazione Comportamentale DoS**:Eseguendo un Monitoraggio attivo dei flussi. Se un host supera la soglia di traffico consentita (`8 Mbps`), il controller installa automaticamente regole di **`DROP` temporanee (30s)** su tutti gli switch, **prescindendo dall'identità dell'host**.

---

## 🛠️ Funzionalità Chiave

- 🔀 **Slicing Dinamico e Isolamento Rigido**: I flussi non autorizzati o cross-slice (es. client Web verso server Video) vengono automaticamente scartati.
- ⚡ **Instradamento Reattivo (First-Packet)**: Le regole OpenFlow non sono caricate staticamente all'avvio, ma installate dinamicamente al primo pacchetto (`OFPPacketIn`).
- 🛡️ **Rilevamento DoS Comportamentale**: L'algoritmo misura il throughput sul solo switch di uscita (`s4`, DPID 4) per evitare conteggi duplicati lungo il percorso.
- ⏱️ **Auto-Unblock Temporizzato**: Le regole di blocco scadono dopo `30 secondi` (`hard_timeout`), permettendo all'host di riprendere le comunicazioni lecite.
- 📊 **Dashboard Web Live**: Interfaccia Flask con scrittura atomica del file `stats.json`, grafici in Canvas nativo e monitoraggio dello stato del controller.
- 🚀 **Script di Avvio Automatico (`start.sh`)**: Script Bash multi-terminale che inizializza ambienti virtuali, controller, dashboard e Mininet con un solo comando.

---

## 📐 Architettura e Topologia di Rete

La topologia di rete emulata in Mininet include **5 Host**, **4 Switch Open vSwitch (s1-s4)**  con due percorsi fisicamente separati a banda diversa
 e **1 Controller Remoto Ryu** connesso sulla porta `6633`.



### 📋 Mappatura Host e Slices

| Host | Indirizzo IP | Ruolo / Funzione | Slice Associata | Banda Link Accesso |
| :--- | :--- | :--- | :--- | :--- |
| **`H1`** | `10.0.0.1/24` | Client Video Legittimo | **Video Slice** (UDP 9999) | `10 Mbps` |
| **`H2`** | `10.0.0.2/24` | Client Web Legittimo | **Web Slice** (TCP 5001) | `10 Mbps` |
| **`H3`** | `10.0.0.3/24` | Host Generatore / Attacker | **Video Slice** (UDP 9999) | `10 Mbps` |
| **`H4`** | `10.0.0.4/24` | Video Server | **Video Destination** | `10 Mbps` |
| **`H5`** | `10.0.0.5/24` | Web Server | **Web Destination** | `10 Mbps` |

### 🛤️ Percorsi Fisici e Capacità

- **Slice Video (`H1/H3 -> H4`)**: Instradata su **`s1 -> s2 -> s4`** con capacita link inter-switch di **`10 Mbps`**.
- **Slice Web (`H2 -> H5`)**: Instradata su **`s1 -> s3 -> s4`** con capacita link inter-switch di **`3 Mbps`**.

---

## ⚙️ Configurazione del Sistema (`config.json`)

Le soglie operative e i parametri di rete sono gestiti nel file `config.json`:

```json
{
  "video_port": 9999,
  "web_port": 5001,
  "dos_threshold_mbps": 8,
  "block_timeout": 30
}
```

- **`video_port`**: Porta UDP destinata allo streaming video.
- **`web_port`**: Porta TCP destinata al traffico web.
- **`dos_threshold_mbps`**: Soglia massima di throughput (8 Mbps). Oltre questo valore scatta il blocco DoS.
- **`block_timeout`**: Durata in secondi (30s) della regola di DROP.

---

## 💻 Installazione e Requisiti

**Requisiti di Sistema**
- Ubuntu 20.04+
- Python 3.8
- Mininet
- Open vSwitch
- Ryu 4.34
- Flask 2.2.5

**1. Entra nella cartella del progetto**
```bash
cd sdn-service-slicing-dos
```

**2. Configura l'Ambiente Virtuale Python (Con pyenv)**
```bash
pyenv install 3.8.18
pyenv virtualenv 3.8.18 ryu38-env
pyenv activate ryu38-env
pip install -r requirements.txt
```

---


## 🚀 Guida all'Avvio

### Metodo 1: Avvio Automatico (Consigliato)
È fornito uno script Bash `start.sh` che esegue la pulizia di Mininet, apre tre finestre di terminale dedicate, inizializza le variabili d'ambiente ed avvia automaticamente la Dashboard nel browser:
```bash
chmod +x start.sh
./start.sh
```
### Metodo 2: Avvio Manuale (3 Terminali)
Se si desidera analizzare i log dei singoli moduli, aprire 3 finestre di terminale distinte:

**1️⃣ Terminale 1 — Controller Ryu**
```bash
cd sdn-service-slicing-dos
pyenv activate ryu38-env
ryu-manager controller.py --ofp-tcp-listen-port 6633
```

**2️⃣ Terminale 2 — Dashboard Web**
```bash
cd sdn-service-slicing-dos
pyenv activate ryu38-env
python3 dashboard.py
```
🌐 **Dashboard Web**: Apri il browser su `http://127.0.0.1:5050`

**3️⃣ Terminale 3 — Topologia Mininet**
```bash
cd sdn-service-slicing-dos
pyenv activate ryu38-env
sudo python3 topology.py
```
---

## 🧪 Guida ai Test

Nei test vado a:
- Verificare l'isolamento tra slice (traffico non autorizzato bloccato)
- Verificare il funzionamento della slice video e della slice web
- Rilevare e mitigare il traffico anomalo (DoS)
- Verificare il funzionamento della dashboard

Per eseguire dei test all'interno della cartella `tests/` abbiamo:
Dettagli su comandi, risultati attesi e risultati osservati in `tests/test_commands.txt`

---

## 👥 Autori

**Autori e crediti**: 
- Davide Cipriano - M63001780
- Giuseppe De Lucia - M63001783
- Valerio Cera – M63001700