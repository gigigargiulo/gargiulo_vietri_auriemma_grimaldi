#!/usr/bin/env python3
"""
DynLoadBalancer - Automated Test & Experimentation Suite
Corso di Network and Cloud Infrastructures - Prof. Giorgio Ventre - a.a. 2025/2026

Script per l'esecuzione automatizzata dei test di verifica sperimentale
descritti nella Sezione 4 del README di progetto.
"""

import sys
import subprocess
import time
import json
import urllib.request


VIP = "10.0.0.100"


def print_banner(title):
    """Formatta e stampa a video un'intestazione visiva delimitata da caratteri di separazione."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def test_ping():
    """
    TEST A: Verifica Connettività Iniziale, Risoluzione ARP Proxy e Reattività ICMP.
    Invia 4 richieste ICMP Echo verso il Virtual IP (10.0.0.100).
    Verifica che il controller intercetti la richiesta ARP, risponda con il VMAC virtuale
    e installi correttamente le regole bidirezionali di flow mod senza drop di pacchetti.
    """
    print_banner("TEST A: Verifica Connettività Iniziale e Risoluzione ARP")
    print(f"[*] Invio di 4 pacchetti ICMP Echo verso il Virtual IP ({VIP})...")
    
    # Argomenti comando ping:
    # - '-c 4': invia esattamente 4 pacchetti ICMP Echo Request e termina l'esecuzione
    cmd = ["ping", "-c", "4", VIP]
    try:
        # subprocess.run esegue il comando di sistema in un processo figlio separato:
        # - stdout/stderr=subprocess.PIPE: redirige l'output su pipe di memoria per consentirne l'ispezione ed evitare stampe disordinate
        # - text=True: decodifica automaticamente i byte di ritorno in stringhe UTF-8 (evitando .decode('utf-8'))
        # - timeout=10: scade dopo 10 secondi per prevenire blocchi indefiniti in caso di mancata risposta della rete
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        
        # Stampa a video l'output formattato del comando ping (tempi RTT min/avg/max/mdev e packet loss)
        print(res.stdout)
        
        # returncode == 0 indica successo secondo lo standard POSIX (tutti o parte dei pacchetti hanno ricevuto Echo Reply)
        if res.returncode == 0:
            print("[✓] TEST A SUPERATO: Ping verso VIP riuscito senza perdita di pacchetti.")
        else:
            print("[✗] TEST A FALLITO: Impossibile raggiungere il VIP.")
    except Exception as e:
        print(f"[!] Errore esecuzione ping: {e}")


def test_http_requests(num_requests=6):
    """
    TEST B: Verifica Bilanciamento HTTP verso il Cluster Backend.
    Invia richieste HTTP GET sequenziali verso l'indirizzo VIP (http://10.0.0.100/).
    Misura la latenza RTT complessiva (3-way handshake TCP + round trip HTTP),
    interroga la risposta JSON del server per identificare quale replica (srv1, srv2, srv3)
    ha processato la transazione e calcola la distribuzione percentuale di carico.
    """
    print_banner("TEST B: Verifica Bilanciamento HTTP verso il Server Pool")
    print(f"[*] Invio di {num_requests} richieste HTTP sequenziali verso http://{VIP}/ ...")
    server_counts = {}

    for i in range(1, num_requests + 1):
        try:
            # Campiona il timestamp iniziale con precisione al microsecondo
            start_t = time.time()
            
            # Crea l'oggetto Request specificando l'endpoint VIP
            req = urllib.request.Request(f"http://{VIP}/")
            
            # urllib.request.urlopen:
            # 1. Apre la connessione TCP sulla porta 80 del VIP (innescando il Packet-In di tipo TCP SYN sul controller)
            # 2. Invia la richiesta HTTP 'GET / HTTP/1.1'
            # 3. Riceve la risposta con timeout di salvaguardia di 5 secondi
            with urllib.request.urlopen(req, timeout=5) as response:
                # Calcola il tempo di risposta totale in millisecondi (RTT end-to-end)
                elapsed = (time.time() - start_t) * 1000.0
                
                # Legge il payload di risposta, lo decodifica in stringa UTF-8 e deserializza il JSON
                data = json.loads(response.read().decode('utf-8'))
                
                # Estrae i dati identificativi iniettati dall'handler del server backend
                srv = data.get("server_host", "Unknown")
                ip = data.get("server_ip", "Unknown")
                
                # Aggiorna il contatore statistico di assegnazione per il server
                server_counts[srv] = server_counts.get(srv, 0) + 1
                print(f"  -> Req #{i}: Servita da {srv} ({ip}) in {elapsed:.2f} ms")
        except Exception as e:
            print(f"  -> Req #{i}: Errore di connessione ({e})")
            
        # Pausa di 0.5s tra richieste successive per consentire l'osservazione visiva
        # dei log nel terminale del controller Ryu e rispettare l'intervallo di telemetria
        time.sleep(0.5)

    # Elaborazione statistica finale: calcola le quote percentuali gestite da ciascuna replica
    print("\n[*] Distribuzione delle richieste nel cluster backend:")
    for srv, count in server_counts.items():
        percent = (count / num_requests) * 100.0
        print(f"    - {srv}: {count} richieste ({percent:.1f}%)")
    print("[✓] TEST B COMPLETATO.")


def main():
    """
    Funzione principale di orchestrazione dei test.
    Supporta l'esecuzione mirata tramite riga di comando:
    - 'python3 test_client.py ping'  -> esegue solo il test di connettività ICMP
    - 'python3 test_client.py http'  -> esegue solo la sequenza di bilanciamento HTTP
    - senza argomenti                -> esegue l'intera suite sequenziale (A + B)
    """
    print("======================================================================")
    print("    DynLoadBalancer - Suite di Validazione Sperimentale")
    print("======================================================================")
    if len(sys.argv) > 1:
        mode = sys.argv[1].lower()
        if mode == "ping":
            test_ping()
        elif mode == "http":
            test_http_requests()
        else:
            print(f"Comando non riconosciuto: '{mode}'. Opzioni disponibili: ping, http")
    else:
        test_ping()
        time.sleep(1)
        test_http_requests()


if __name__ == '__main__':
    main()
