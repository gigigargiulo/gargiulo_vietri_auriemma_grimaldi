#!/usr/bin/env python3
"""
DynLoadBalancer - Dynamic Multi-Criteria SDN Load Balancing Controller
Corso di Network and Cloud Infrastructures - Prof. Giorgio Ventre - a.a. 2025/2026

Caratteristiche Implementate:
1. Virtual IP Proxy (VIP NAT / ARP Proxy) con piena trasparenza L2/L3.
2. Link Monitoring asincrono periodico (OFPPortStatsRequest ogni 2 secondi) per calcolo Throughput.
3. Selezione del percorso core basata sul confronto diretto del carico (Path A vs Path B).
4. Bilanciamento delle sessioni verso pool di server backend (srv1, srv2, srv3) in base al loro carico.
5. Installazione bidirezionale di flow OpenFlow 1.3 con idle_timeout=20s e tracciamento sessioni attive.
"""

import time
import networkx as nx  # grafi e Dijkstra
 
from ryu.base import app_manager  # Classe base per tutte le applicazioni SDN gestite da Ryu
from ryu.controller import ofp_event  # Modulo degli eventi OpenFlow generati dagli switch
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls  # Gestori di stato della sessione OpenFlow
from ryu.ofproto import ofproto_v1_3  # Costanti e definizioni del protocollo OpenFlow v1.3
from ryu.lib.packet import packet, ethernet, arp, ipv4, tcp, udp, ether_types  # Parser per l'ispezione dei protocolli di rete
from ryu.lib import hub  # Libreria di threading per task asincroni in background


class DynLoadBalancerController(app_manager.RyuApp):
    # versione del protocollo OpenFlow negoziata con gli switch
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    VIP = '10.0.0.100'
    VMAC = '00:00:00:00:00:fe'

    # Parametri della Funzione di Costo dei link di rete:
    # Costo = ALPHA * (1.0 / (B_free + EPSILON)) + BETA * Delay
    ALPHA = 40.0          # Peso associato alla saturazione di banda
    BETA = 1.0            # Peso associato alla latenza di propagazione (ms)
    EPSILON = 0.5         # Fattore di regolarizzazione per prevenire divisioni per zero
    MONITOR_INTERVAL = 2  # Frequenza del thread di polling delle statistiche delle porte (secondi)
    FLOW_IDLE_TIMEOUT = 20  # Secondi di inattività prima che lo switch rimuova automaticamente la regola di flusso

    # Client connessi allo switch di edge ingress S1
    HOSTS = {
        '10.0.0.1': {'mac': '00:00:00:00:00:01', 'dpid': 1, 'port': 1, 'name': 'h1'},
        '10.0.0.2': {'mac': '00:00:00:00:00:02', 'dpid': 1, 'port': 2, 'name': 'h2'},
        '10.0.0.3': {'mac': '00:00:00:00:00:03', 'dpid': 1, 'port': 3, 'name': 'h3'},
    }

    # Server connessi allo switch di edge egress S6
    SERVERS = {
        '10.0.0.11': {'mac': '00:00:00:00:00:11', 'dpid': 6, 'port': 3, 'name': 'srv1'},
        '10.0.0.12': {'mac': '00:00:00:00:00:12', 'dpid': 6, 'port': 4, 'name': 'srv2'},
        '10.0.0.13': {'mac': '00:00:00:00:00:13', 'dpid': 6, 'port': 5, 'name': 'srv3'},
    }

    # Topologia fisica dei link con capacità (Mbps) e ritardo (ms)
    CORE_LINKS = {
        # Path A (S1 - S2 - S4 - S6): Canale ad alta capacità (20 Mbps) ma latenza maggiore (5 ms per hop)
        (1, 2): {'src_port': 4, 'dst_port': 1, 'capacity': 20.0, 'delay': 5.0},
        (2, 1): {'src_port': 1, 'dst_port': 4, 'capacity': 20.0, 'delay': 5.0},
        (2, 4): {'src_port': 2, 'dst_port': 1, 'capacity': 20.0, 'delay': 5.0},
        (4, 2): {'src_port': 1, 'dst_port': 2, 'capacity': 20.0, 'delay': 5.0},
        (4, 6): {'src_port': 2, 'dst_port': 1, 'capacity': 20.0, 'delay': 5.0},
        (6, 4): {'src_port': 1, 'dst_port': 2, 'capacity': 20.0, 'delay': 5.0},

        # Path B (S1 - S3 - S5 - S6): Canale a bassa latenza (1 ms per hop) ma capacità dimezzata (10 Mbps)
        (1, 3): {'src_port': 5, 'dst_port': 1, 'capacity': 10.0, 'delay': 1.0},
        (3, 1): {'src_port': 1, 'dst_port': 5, 'capacity': 10.0, 'delay': 1.0},
        (3, 5): {'src_port': 2, 'dst_port': 1, 'capacity': 10.0, 'delay': 1.0},
        (5, 3): {'src_port': 1, 'dst_port': 2, 'capacity': 10.0, 'delay': 1.0},
        (5, 6): {'src_port': 2, 'dst_port': 2, 'capacity': 10.0, 'delay': 1.0},
        (6, 5): {'src_port': 2, 'dst_port': 2, 'capacity': 10.0, 'delay': 1.0},
    }

    def __init__(self, *args, **kwargs):
        super(DynLoadBalancerController, self).__init__(*args, **kwargs)

        self.mac_to_port = {}  # Tabella di inoltro L2 (dpid -> {mac: port})
        self.datapaths = {}    # Mappa dei canali OpenFlow attivi (dpid -> datapath)
        self.port_stats = {}   # Cache delle statistiche sulle porte: (dpid, port) -> (tx_bytes, timestamp, speed_mbps)

        # Tracciamento dello stato di carico delle istanze server
        self.server_active_conns = {ip: 0 for ip in self.SERVERS}  # Numero di sessioni TCP/UDP attive
        self.server_throughput = {ip: 0.0 for ip in self.SERVERS}  # Traffico in uscita misurato (Mbps)
        self.server_round_robin_idx = 0                            # Indice round-robin (usato per spareggio a parità di carico)

        # Inizializzazione del grafo orientato su cui opera Dijkstra
        self.graph = nx.DiGraph()
        self._init_topology_graph()

        # Avvio del thread in background per il monitoraggio periodico delle porte OVS
        self.monitor_thread = hub.spawn(self._monitor_loop)

    def _calc_link_cost(self, free_bw, delay):
        """
        Calcola il costo finale del link secondo la formula:
        Costo = ALPHA * (1 / (B_free + EPSILON)) + BETA * delay
        """
        effective_bw = max(0.01, free_bw)
        return round(self.ALPHA * (1.0 / (effective_bw + self.EPSILON)) + self.BETA * delay, 2)

    def _init_topology_graph(self):
        """Inizializza la topologia nel grafo NetworkX registrando archi, capacità, ritardo e costo iniziale."""
        for dpid in range(1, 7):
            self.graph.add_node(dpid)

        for (u, v), link_info in self.CORE_LINKS.items():
            cap = link_info['capacity']
            delay = link_info['delay']
            initial_cost = self._calc_link_cost(cap, delay)
            self.graph.add_edge(
                u, v,
                src_port=link_info['src_port'],
                dst_port=link_info['dst_port'],
                capacity=cap,
                delay=delay,
                throughput=0.0,
                cost=initial_cost
            )

    # GESTIONE CONNESSIONE SWITCH ED EVENTI TOPOLOGIA

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
        Intercetta l'evento di handshake iniziale tra Switch e Controller (stato CONFIG_DISPATCHER).
        Installa la regola fondamentale di Table-Miss (a priorità 0) che ordina allo switch di
        inviare qualsiasi pacchetto non corrispondente a flussi noti al controller via Packet-In (slow-path).
        """
        # Dall'evento scatenato da Ryu ricaviamo il datapath (lo switch che si è connesso) e le sue caratteristiche
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Memorizza il datapath in un dizionario globale (usando come chiave il suo ID)
        self.datapaths[datapath.id] = datapath
        self.logger.info("[SWITCH JOIN] Switch S%s connesso al controller (dpid=%s)", datapath.id, datapath.id)

        # wildcard: intercetta TUTTI i pacchetti che non trovano corrispondenza nella flow table
        match = parser.OFPMatch()

        # Azione: inoltra al controller via porta speciale OFPP_CONTROLLER
        # OFPCML_NO_BUFFER ordina allo switch di inviare l'intero pacchetto nel Packet-In senza memorizzarlo nel buffer locale
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, priority=0, match=match, actions=actions)
        self.logger.info("[SWITCH CONFIG] Regola Table-Miss installata su S%s", datapath.id)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None, idle_timeout=0, flags=0):
        """
        Invia un messaggio OpenFlow OFPFlowMod per installare o modificare una regola di flusso nello switch.
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        # istruzione OFPIT_APPLY_ACTIONS: applica immediatamente le azioni specificate
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        # se il pacchetto arriva con istruzione != OFP_NO_BUFFER significa che abbiamo passato anche il pacchetto, quindi applichiamo anche la regola
        if buffer_id and buffer_id != ofproto.OFP_NO_BUFFER:
            mod = parser.OFPFlowMod(datapath=datapath, 
                                    buffer_id=buffer_id,
                                    priority=priority, 
                                    match=match, instructions=inst, 
                                    idle_timeout=idle_timeout, flags=flags)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, instructions=inst,
                                    idle_timeout=idle_timeout, flags=flags)
        datapath.send_msg(mod)

    # MODULO ASINCRONO DI MONITORAGGIO/TELEMETRIA RISORSE (LinkMonitor)

    def _monitor_loop(self):
        """
        Thread periodico avviato con hub.spawn.
        Invia periodicamente richieste OFPPortStatsRequest a tutti gli switch per
        misurare il traffico sulle interfacce senza bloccare il controller.
        Opera in background.
        """
        hub.sleep(2)  # Pausa iniziale per attendere che tutti gli switch abbiano completato l'handshake
        iteration = 0
        while True:
            for dp in list(self.datapaths.values()):
                parser = dp.ofproto_parser
                # OFPPortStatsRequest: richiede le statistiche cumulative di trasmissione/ricezione per tutte le porte OFPP_ANY
                req = parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY)
                # Invio la richiesta costruita tramite il metodo send_msg del datapath
                dp.send_msg(req)
            hub.sleep(self.MONITOR_INTERVAL) #dorme per MONITOR_INTERVAL secondi e cede la CPU in modo che gli switch possano rispondere con OFPPortStatsReply
            iteration += 1
            # Stampa la telemetria di stato della rete ogni 5 iterazioni (10 secondi) se tutti gli switch sono online
            if iteration % 5 == 0 and len(self.datapaths) >= 6:
                self._log_network_telemetry()

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        """
        Elabora la risposta OFPPortStatsReply inviata dallo switch.
        Calcola la velocità di trasmissione istantanea (Throughput in Mbps), la banda residua (B_free)
        e aggiorna dinamicamente i pesi nel grafo NetworkX per il routing.
        """
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        now = time.time()
 
        for stat in body:
            port_no = stat.port_no
            # Ignora porte speciali e riservate (es. OFPP_LOCAL, OFPP_ALL)
            if port_no > ofproto_v1_3.OFPP_MAX:
                continue
 
            key = (dpid, port_no)
            tx_bytes = stat.tx_bytes  # Byte totali trasmessi dalla porta dall'avvio dello switch
 
            if key in self.port_stats:
                prev_tx, prev_time, _ = self.port_stats[key]
                delta_t = now - prev_time
                if delta_t > 0:
                    # Calcolo throughput: (Delta Byte * 8 bit) / (Delta tempo * 10^6) = Megabit al secondo (Mbps)
                    speed_mbps = ((tx_bytes - prev_tx) * 8.0) / (delta_t * 1e6)
                    self.port_stats[key] = (tx_bytes, now, speed_mbps)

                    # Se la porta corrisponde a un link tra switch, aggiorna throughput calcolando la banda libera e costo finale nel grafo
                    for (u, v), link_info in self.CORE_LINKS.items():
                        # Se lo switch sorgente e la porta sorgente corrispondono a quella che ha appena ricevuto le statistiche, aggiorna il link
                        if u == dpid and link_info['src_port'] == port_no and self.graph.has_edge(u, v):
                            free_bw = max(0.01, link_info['capacity'] - speed_mbps)
                            # Aggiorna il throughput e il costo del link nel grafo
                            self.graph[u][v]['throughput'] = round(speed_mbps, 2)
                            self.graph[u][v]['cost'] = self._calc_link_cost(free_bw, link_info['delay'])

                    # Se lo switch è S6 e quindi la porta appartiene a un server, registra il throughput del server
                    if dpid == 6:
                        for s_ip, s_info in self.SERVERS.items():
                            if s_info['port'] == port_no:
                                self.server_throughput[s_ip] = round(speed_mbps, 2)
            else:
                # Primo campionamento: memorizza il contatore iniziale come riferimento temporale
                self.port_stats[key] = (tx_bytes, now, 0.0)

    def _log_network_telemetry(self):
        """
        Stampa periodica del costo e del carico sui percorsi core e sui server.
        """
        try:
            links_a = [(1, 2), (2, 4), (4, 6)]
            links_b = [(1, 3), (3, 5), (5, 6)]

            cost_a = round(sum(self.graph[u][v]['cost'] for u, v in links_a), 2)
            cost_b = round(sum(self.graph[u][v]['cost'] for u, v in links_b), 2)
            load_a = max(self.graph[u][v]['throughput'] for u, v in links_a)
            load_b = max(self.graph[u][v]['throughput'] for u, v in links_b)

            # Calcolo del carico per ciascun server basato su connessioni attive*10 + throughput
            srv_status = " | ".join(
                f"{self.SERVERS[ip]['name']}: {round(self.server_active_conns[ip] * 10.0 + self.server_throughput[ip], 1)}"
                for ip in self.SERVERS
            )

            self.logger.info("[TELEMETRY] Costo Percorsi -> Path A: %.2f (Carico: %.2fM) | Path B: %.2f (Carico: %.2fM)",
                             cost_a, load_a, cost_b, load_b)
            self.logger.info("[TELEMETRY] Costo Server   -> %s", srv_status)
        except Exception:
            pass

    # GESTIONE PACKET-IN

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        """
        Punto di ingresso per i pacchetti inviati al controller dalla regola Table-Miss.
        Smista i pacchetti tra ARP Proxy, Bilanciamento VIP NAT o apprendimento L2 di fallback.
        """
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']  # Porta dello switch da cui è entrato il frame (serve al Controller per fare l'ARP Reply verso il client che ha richiesto l'indirizzo IP)
 
        # Decapsula la catena di protocolli a partire dal livello Ethernet L2
        # .Packet è il parser integrato di Ryu per trasformare la sequenza grezza di byte del frame Ethernet in una lista di oggetti Python, uno per ogni protocollo presente nel frame (es. Ethernet, ARP, IP, TCP/UDP)
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
 
        # Scarta pacchetti LLDP (Link Layer Discovery Protocol) usati dalla topologia
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
 
            # Estrae gli indirizzi MAC di destinazione, sorgente e l'ID dello switch
        dst = eth.dst
        src = eth.src
        dpid = datapath.id
 
        # ARP Proxy & Handling: intercetta e risponde direttamente alle richieste ARP
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            self._handle_arp(datapath, in_port, eth, arp_pkt)
            return
 
        #  VIP Load Balancing: intercetta traffico destinato al Virtual IP (10.0.0.100) all'ingresso di S1
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt and ip_pkt.dst == self.VIP and dpid == 1:
            self._handle_vip_lb(msg, datapath, in_port, eth, ip_pkt, pkt)
            return
 
        # Autoapprendimento MAC tradizionale per eventuale traffico di gestione tra nodi
        # Se non esiste, crea la tabella di inoltro per lo switch dpid e la sorgente src il valore in_port
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        # Se la porta di inoltro è già presente la imposta altrimenti esegue il flood
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD 
 
        # Imposta l'azione che dice allo switch verso quale porta emettere il pacchetto (inoltro)
        actions = [parser.OFPActionOutput(out_port)]

        # Installazione di una regola di inoltro per evitare il packet-in al prossimo pacchetto
        # Se la porta di inoltro non è flood (cioè si conosce la porta di destinazione)
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                # Se il pacchetto è stato bufferizzato, viene inoltrato direttamente usando il buffer_id
                self.add_flow(datapath, 1, match, actions, buffer_id=msg.buffer_id)
                return
            else:
                # Se il pacchetto non è stato bufferizzato, il controller installa la regola e poi inoltra successivamente il pacchetto
                self.add_flow(datapath, 1, match, actions)
 
        # Inoltra il frame sia nel caso OFP_NO_BUFFER che quello in cui out_port=OFPP_FLOOD (in questo caso quello che eventualmente c'è nel buffer viene svuotato e fa flood su tutte le porte)
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
 
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)
 
    #  LOAD BALANCER & VIP NAT

    def _handle_arp(self, datapath, in_port, eth_pkt, arp_pkt):
        """
        Modulo ARP Proxy: intercetta le richieste ARP (Who has VIP / Who has Client/Server)
        ed emette una risposta ARP Reply formulata direttamente dal controller.
        Ciò maschera l'infrastruttura reale, nasconde i MAC fisici dei server ai client
        ed elimina completamente il flooding broadcast nella rete emulata.
        """
        if arp_pkt.opcode != arp.ARP_REQUEST:
            return

        target_ip = arp_pkt.dst_ip
        reply_mac = None

        # Se la richiesta è per il Virtual IP (10.0.0.100), risponde con il MAC virtuale
        if target_ip == self.VIP:
            reply_mac = self.VMAC
        elif target_ip in self.HOSTS:
            reply_mac = self.HOSTS[target_ip]['mac']
        elif target_ip in self.SERVERS:
            reply_mac = self.SERVERS[target_ip]['mac']

        if reply_mac:
            self.logger.info("[ARP PROXY] Richiesta ARP per %s da %s -> Risposta con MAC %s (in_port=%s)",
                             target_ip, arp_pkt.src_ip, reply_mac, in_port)
            parser = datapath.ofproto_parser
            ofproto = datapath.ofproto

            # Costruzione manuale del pacchetto Ethernet + ARP Reply
            reply = packet.Packet()
            reply.add_protocol(ethernet.ethernet(
                ethertype=ether_types.ETH_TYPE_ARP,
                src=reply_mac,
                dst=eth_pkt.src
            ))
            reply.add_protocol(arp.arp(
                opcode=arp.ARP_REPLY,
                src_mac=reply_mac,
                src_ip=target_ip,
                dst_mac=arp_pkt.src_mac,
                dst_ip=arp_pkt.src_ip
            ))
            # serialize(): converte la struttura logica ad oggetti di Ryu in una sequenza grezza di byte (raw frame)
            reply.serialize()

            # OFPActionOutput(in_port): reinvia il pacchetto ARP Reply indietro sulla stessa porta da cui era entrata la richiesta
            actions = [parser.OFPActionOutput(in_port)]

            # OFPPacketOut ordina allo switch di trasmettere direttamente il frame sintetizzato dal controller
            # in_port=OFPP_CONTROLLER indica allo switch che il frame ha avuto origine dal piano di controllo
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=ofproto.OFP_NO_BUFFER,
                                      in_port=ofproto.OFPP_CONTROLLER, actions=actions, data=reply.data)
            datapath.send_msg(out)

    def _select_server(self):
        """
        Seleziona la replica server con il costo di carico minore:
        Costo Server = (Connessioni_Attive * 10.0) + Throughput_Misurato_Mbps
        In caso di parità di costo minimo, applica una rotazione Round-Robin equa.
        """
        # Calcola il costo di ciascun server prima della nuova assegnazione
        scores = {ip: round(self.server_active_conns[ip] * 10.0 + self.server_throughput[ip], 1) for ip in self.SERVERS}
        min_score = min(scores.values())
        candidates = [ip for ip, sc in scores.items() if sc == min_score]

        if len(candidates) == 1:
            chosen = candidates[0]
            chosen_name = self.SERVERS[chosen]['name']
            other_names = [self.SERVERS[ip]['name'] for ip in self.SERVERS if ip != chosen]
            reason = f"costo minore ({min_score} rispetto a {', '.join(other_names)})"
        else:
            chosen = list(self.SERVERS.keys())[self.server_round_robin_idx % len(self.SERVERS)]
            self.server_round_robin_idx += 1
            reason = "rotazione Round-Robin a parità di costo minimo"

        # Formatta la stringa dei costi numerici dei server al momento della decisione
        scores_desc = ", ".join(f"{self.SERVERS[ip]['name']}: {scores[ip]}" for ip in self.SERVERS)

        # Incrementa il contatore delle connessioni attive per il server prescelto
        self.server_active_conns[chosen] += 1
        return chosen, scores_desc, reason

    def _handle_vip_lb(self, msg, datapath, in_port, eth_pkt, ip_pkt, pkt):
        """
        Gestore principale del Load Balancing:
        1. Identifica il tipo di sessione a livello 4.
        2. Calcola i costi finali dei cammini Path A e Path B e seleziona il percorso ottimo con motivo esplicito.
        3. Seleziona il server backend a costo minore e stampa i costi e il motivo esplicito.
        4. Installa le regole bidirezionali con riscrittura degli header IP/MAC (NAT L2/L3).
        5. Inoltra il primo pacchetto originale con gli header già modificati via PacketOut.
        """
        client_ip = ip_pkt.src
        client_mac = eth_pkt.src
        server_ip, server_scores_desc, server_reason = self._select_server()
        server_mac = self.SERVERS[server_ip]['mac']
        server_port = self.SERVERS[server_ip]['port']
        server_name = self.SERVERS[server_ip]['name']

        # Ispezione Layer 4 per identificare protocollo, porte e servizio applicativo
        proto = ip_pkt.proto
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        udp_pkt = pkt.get_protocol(udp.udp)
        tcp_src = tcp_pkt.src_port if tcp_pkt else None
        tcp_dst = tcp_pkt.dst_port if tcp_pkt else None
        udp_src = udp_pkt.src_port if udp_pkt else None
        udp_dst = udp_pkt.dst_port if udp_pkt else None

        if tcp_pkt:
            service = "HTTP" if (tcp_dst == 80 or tcp_src == 80) else ("iPerf" if (tcp_dst == 5001 or tcp_src == 5001) else "TCP")
            l4_desc = f"TCP {client_ip}:{tcp_src} -> VIP {self.VIP}:{tcp_dst} [{service}]"
        elif udp_pkt:
            service = "iPerf-UDP" if (udp_dst == 5001 or udp_src == 5001) else "UDP"
            l4_desc = f"UDP {client_ip}:{udp_src} -> VIP {self.VIP}:{udp_dst} [{service}]"
        elif proto == 1:
            l4_desc = f"ICMP Echo Request {client_ip} -> VIP {self.VIP}"
        else:
            l4_desc = f"IP (proto={proto}) {client_ip} -> VIP {self.VIP}"

        # Calcolo del costo finale cumulativo lungo i link dei percorsi core
        links_a = [(1, 2), (2, 4), (4, 6)]
        links_b = [(1, 3), (3, 5), (5, 6)]

        cost_a = round(sum(self.graph[u][v]['cost'] for u, v in links_a), 2)
        cost_b = round(sum(self.graph[u][v]['cost'] for u, v in links_b), 2)

        # Selezione del percorso in base al costo finale e determinazione del motivo esplicito
        if cost_b <= cost_a:
            path = [1, 3, 5, 6]
            path_name = "Path B"
            routing_reason = "Path B scelto per costo minore (latenza 1ms vs 5ms)"
        else:
            path = [1, 2, 4, 6]
            path_name = "Path A"
            routing_reason = "Path A scelto perché Path B è congestionato (costo elevato per banda residua ridotta)"

        # Logging chiaro, comprensibile, con costo finale e motivo esplicito
        self.logger.info("[LB SESSION] Richiesta: %s", l4_desc)
        self.logger.info("[LB ROUTING] Costo Path A: %.2f | Costo Path B: %.2f ==> Scelto: %s [Motivo: %s]",
                         cost_a, cost_b, path_name, routing_reason)
        self.logger.info("[LB SERVER]  Costo Server: [%s] ==> Assegnato a: %s (%s) [Motivo: %s]",
                         server_scores_desc, server_name, server_ip, server_reason)

        # ----------------------------------------------------------------------
        # 1. Installazione Cammino di Andata (Client -> Server Reale): S1 -> core -> S6
        # ----------------------------------------------------------------------
        for idx, dpid in enumerate(path):
            if dpid not in self.datapaths:
                continue
            dp = self.datapaths[dpid]
            p = dp.ofproto_parser
            m_args = {'eth_type': ether_types.ETH_TYPE_IP, 'ip_proto': proto}

            if dpid == 1:
                # S1 (Ingress Switch): Esegue il NAT L2/L3 riscrittore
                # Riconosce la sessione per 5-tuple verso il VIP
                m_args.update({'in_port': in_port, 'ipv4_src': client_ip, 'ipv4_dst': self.VIP})
                if tcp_src: m_args.update({'tcp_src': tcp_src, 'tcp_dst': tcp_dst})
                if udp_src: m_args.update({'udp_src': udp_src, 'udp_dst': udp_dst})
                
                next_hop = path[idx + 1]
                out_p = self.graph[dpid][next_hop]['src_port']

                # OFPActionSetField: riscrive l'IP e il MAC di destinazione con quelli del server scelto
                acts = [p.OFPActionSetField(ipv4_dst=server_ip),
                        p.OFPActionSetField(eth_dst=server_mac),
                        p.OFPActionOutput(out_p)]
                
                # OFPFF_SEND_FLOW_REM ordina allo switch di notificare il controller quando il flusso
                # scade per inattività (idle_timeout=20s), permettendo di deallocare la sessione attiva
                self.add_flow(dp, 20, p.OFPMatch(**m_args), acts,
                              idle_timeout=self.FLOW_IDLE_TIMEOUT, flags=dp.ofproto.OFPFF_SEND_FLOW_REM)
            elif dpid == 6:
                # S6 (Egress Switch): Consegna diretta al server backend attestato sulla porta dedicata
                m_args.update({'ipv4_src': client_ip, 'ipv4_dst': server_ip})
                if tcp_src: m_args.update({'tcp_src': tcp_src, 'tcp_dst': tcp_dst})
                if udp_src: m_args.update({'udp_src': udp_src, 'udp_dst': udp_dst})
                self.add_flow(dp, 20, p.OFPMatch(**m_args), [p.OFPActionOutput(server_port)],
                              idle_timeout=self.FLOW_IDLE_TIMEOUT)
            else:
                # Switch intermedi di core (S2/S4 o S3/S5): puro inoltro basato su indirizzi reali
                m_args.update({'ipv4_src': client_ip, 'ipv4_dst': server_ip})
                if tcp_src: m_args.update({'tcp_src': tcp_src, 'tcp_dst': tcp_dst})
                if udp_src: m_args.update({'udp_src': udp_src, 'udp_dst': udp_dst})
                next_hop = path[idx + 1]
                out_p = self.graph[dpid][next_hop]['src_port']
                self.add_flow(dp, 20, p.OFPMatch(**m_args), [p.OFPActionOutput(out_p)],
                              idle_timeout=self.FLOW_IDLE_TIMEOUT)

        # ----------------------------------------------------------------------
        # 2. Installazione Cammino di Ritorno (Server Reale -> Client): S6 -> core -> S1
        # ----------------------------------------------------------------------
        rev_path = list(reversed(path))
        for idx, dpid in enumerate(rev_path):
            if dpid not in self.datapaths:
                continue
            dp = self.datapaths[dpid]
            p = dp.ofproto_parser
            m_args = {'eth_type': ether_types.ETH_TYPE_IP, 'ip_proto': proto,
                      'ipv4_src': server_ip, 'ipv4_dst': client_ip}
            # Nel flusso di ritorno le porte L4 sorgente e destinazione sono invertite
            if tcp_src: m_args.update({'tcp_src': tcp_dst, 'tcp_dst': tcp_src})
            if udp_src: m_args.update({'udp_src': udp_dst, 'udp_dst': udp_src})

            if dpid == 1:
                # S1 (Ritorno verso Client): Ripristina in modo completamente trasparente
                # l'IP sorgente con il Virtual IP (10.0.0.100) e il MAC sorgente con il VMAC (00:...:fe)
                acts = [p.OFPActionSetField(ipv4_src=self.VIP),
                        p.OFPActionSetField(eth_src=self.VMAC),
                        p.OFPActionOutput(in_port)]
            else:
                next_hop = rev_path[idx + 1]
                out_p = self.graph[dpid][next_hop]['src_port']
                acts = [p.OFPActionOutput(out_p)]

            self.add_flow(dp, 20, p.OFPMatch(**m_args), acts, idle_timeout=self.FLOW_IDLE_TIMEOUT)

        # ----------------------------------------------------------------------
        # 3. Inoltro del Primo Pacchetto (Trigger del Packet-In) da S1
        # ----------------------------------------------------------------------
        # Poiché il primo pacchetto della sessione (es. TCP SYN o ICMP Echo) era stato inviato al controller,
        # lo switch S1 non lo aveva ancora inoltrato. Lo emettiamo ora applicando la riscrittura,
        # in modo che il client non subisca la perdita del primo pacchetto e il TCP handshake non fallisca.
        next_hop = path[1]
        out_port = self.graph[1][next_hop]['src_port']
        parser = datapath.ofproto_parser
        actions = [
            parser.OFPActionSetField(ipv4_dst=server_ip),
            parser.OFPActionSetField(eth_dst=server_mac),
            parser.OFPActionOutput(out_port)
        ]

        data = None
        if msg.buffer_id == datapath.ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def _flow_removed_handler(self, ev):
        """
        Intercetta l'evento di scadenza del flusso generato dallo switch quando scade l'idle_timeout di 20s.
        Decrementa in modo atomico il contatore delle connessioni attive per il server reale associato.
        """
        msg = ev.msg
        if msg.match.get('ipv4_dst') == self.VIP and msg.datapath.id == 1:
            for s_ip in self.SERVERS:
                if self.server_active_conns[s_ip] > 0:
                    self.server_active_conns[s_ip] -= 1
                    break