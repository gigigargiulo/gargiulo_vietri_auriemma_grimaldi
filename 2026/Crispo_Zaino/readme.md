# DynLoadBalancer: Bilanciamento Dinamico SDN con Virtual IP basato su banda e latenza

Progetto per il corso di Networks and Cloud Infrastructures 

Membri
Crispo Pasquale (matricola DE9000040)
Zaino Claudia (matricola DE9000149)

---

Obiettivi del Progetto
Nei sistemi di rete tradizionali le richieste dei client vengono distribuite tra i server usando algoritmi statici come il Round-Robin, senza però considerare se i link della rete sono intasati o se c'è troppo ritardo.

L'obiettivo del nostro progetto è sviluppare un'applicazione su controller Ryu (utilizzando OpenFlow 1.3) che non si limiti a smistare le connessioni tra i server, ma tenga conto in tempo reale delle condizioni della rete di trasporto, i passi seguiti sono:
1. Mascherare i server reali esponendo un unico Virtual IP (`10.0.0.100`): il controller gestisce le richieste ARP (Proxy ARP) e riscrive al volo gli header IP e MAC tramite `OFPActionSetField`.
2. Monitorare lo stato dei link tramite un thread asincrono che ogni 2 secondi invia richieste `OFPPortStatsRequest` agli switch per calcolare il throughput in Mbps e la banda residua (B_free).
3. Scegliere il percorso migliore con una funzione di costo che bilancia banda libera e latenza, passando automaticamente da Path B a Path A in caso di congestione.
4. Assegnare le nuove connessioni al server backend con minor carico complessivo, usando Round-Robin in caso di spareggio (a parità di carico).

---

Architettura e Topologia di Rete
Per testare e validare il sistema abbiamo creato su Mininet una topologia composta da:
- 6 switch Open vSwitch: S1 all'ingresso (lato client), S6 all'uscita (lato server) e 4 core switch (S2, S3, S4, S5);
- Due percorsi alternativi tra client e server con caratteristiche asimmetriche:
  - Path A (S1 - S2 - S4 - S6): banda 20 Mbps, ritardo 5 ms per link;
  - Path B (S1 - S3 - S5 - S6): banda 10 Mbps, ritardo 1 ms per link;
- 3 host Client collegati a S1:
  - `h1` (`10.0.0.1` - `00:00:00:00:00:01`)
  - `h2` (`10.0.0.2` - `00:00:00:00:00:02`)
  - `h3` (`10.0.0.3` - `00:00:00:00:00:03`)
- 3 server collegati a S6:
  - `srv1` (`10.0.0.11` - `00:00:00:00:00:11`)
  - `srv2` (`10.0.0.12` - `00:00:00:00:00:12`)
  - `srv3` (`10.0.0.13` - `00:00:00:00:00:13`)
- Un servizio virtuale con VIP `10.0.0.100` e MAC virtuale `00:00:00:00:00:FE`.

---

Prerequisiti Ambiente
- Macchina Virtuale Ubuntu 22.04 LTS
- Mininet con supporto Open vSwitch
- COntroller Ryu
- iperf

---

Istruzioni per l'Avvio

1. Avviare il Controller Ryu (Terminale 1):
	cd ~/Progetto_NCIS
	source venv/bin/activate
	ryu-manager dynamic_lb_controller.py

2. Avviare la Topologia Mininet (Terminale 2):
	cd ~/Progetto_NCIS
	sudo python3 topology_lb.py

3. Esecuzione dei Test di Validazione e Bilanciamento
		Avvio dei server HTTP di backend in background:
			srv1 python3 server_backend.py 80 &
			srv2 python3 server_backend.py 80 &
			srv3 python3 server_backend.py 80 &

		Test di connettività e bilanciamento delle richieste HTTP:
			h1 python3 test_client.py
      
		Simulazione di congestione su Path B con iperf (flusso a 9 Mbps per 40s):
			srv1 iperf -s -p 5001 &
			h1 iperf -c 10.0.0.100 -p 5001 -t 40 -b 9M &
		Verifica del failover automatico su Path A:
			h2 python3 test_client.py http
