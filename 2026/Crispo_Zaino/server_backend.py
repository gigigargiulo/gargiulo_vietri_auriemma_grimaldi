#!/usr/bin/env python3
"""
DynLoadBalancer - Backend HTTP Service
Corso di Network and Cloud Infrastructures - Prof. Giorgio Ventre - a.a. 2025/2026

Fornisce un microservizio HTTP leggero e deterministico da eseguire sulle repliche server (srv1, srv2, srv3).
Risponde alle richieste GET / con un payload JSON che identifica l'IP e il nome del server
che ha effettivamente gestito la sessione, consentendo la validazione sperimentale del bilanciamento SDN.
"""

import sys
import socket
import datetime
import json
from http.server import HTTPServer, BaseHTTPRequestHandler


class LBBackendHandler(BaseHTTPRequestHandler):
    """
    Handler HTTP personalizzato per rispondere a richieste REST/JSON di validazione.
    """

    def do_GET(self):
        """
        Gestisce le richieste HTTP GET in arrivo dai client attraverso il Virtual IP (10.0.0.100).
        """
        try:
            # self.connection.getsockname()[0]: interroga direttamente il socket TCP stabilito
            # per estrarre l'indirizzo IP locale dell'interfaccia di rete su cui è atterrata la connessione.
            # Questo garantisce l'identificazione precisa anche se gli host Mininet condividono lo stesso hostname di sistema.
            local_ip = self.connection.getsockname()[0]
        except Exception:
            local_ip = "10.0.0.1x"

        # Mappatura deterministica tra l'IP di backend reale e il nome dell'host Mininet
        ip_to_name = {
            '10.0.0.11': 'srv1',
            '10.0.0.12': 'srv2',
            '10.0.0.13': 'srv3'
        }
        server_name = ip_to_name.get(local_ip, socket.gethostname())

        # Costruzione del payload di risposta con metadati temporali e di sessione
        response_data = {
            "status": "OK",
            "server_host": server_name,
            "server_ip": local_ip,
            "client_address": self.client_address[0],  # IP reale del client (grazie alla preservazione NAT)
            "client_port": self.client_address[1],     # Porta effimera sorgente del client
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "message": f"Risposta servita con successo dal server backend {server_name} ({local_ip})"
        }

        # Serializzazione della risposta in formato JSON UTF-8
        body = json.dumps(response_data, indent=2).encode('utf-8')
        
        # Invio codice di stato HTTP 200 OK e intestazioni di risposta
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        # Connection: close ordina di chiudere la sessione TCP dopo la risposta,
        # consentendo a nuove richieste di innescare nuovamente la selezione del bilanciatore
        self.send_header('Connection', 'close')
        self.end_headers()
        
        # Scrittura del corpo della risposta sul socket di output
        self.wfile.write(body)

    def log_message(self, format, *args):
        """
        Formatta i log delle richieste su stderr con timestamp pulito per il monitoraggio.
        """
        sys.stderr.write(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {self.client_address[0]} - {args[0]}\n")


def run(port=80):
    """
    Inizializza e avvia il server HTTP in ascolto su tutte le interfacce ('0.0.0.0') alla porta specificata.
    """
    server_address = ('0.0.0.0', port)
    httpd = HTTPServer(server_address, LBBackendHandler)
    hostname = socket.gethostname()
    print(f"=== Backend HTTP Server avviato su {hostname} (porta {port}) ===")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nArresto del server.")
        httpd.server_close()


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    run(port)
