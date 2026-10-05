#!/usr/bin/env python3

import json
import time
import os
import tempfile
from collections import deque

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ether_types, arp, ipv4, tcp, udp
from ryu.ofproto import ofproto_v1_3


class ServiceSlicingController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # Definizione statica degli indirizzi IP dei 5 host emulati nella topologia

    H1 = "10.0.0.1"  # client video
    H2 = "10.0.0.2"  # client web
    H3 = "10.0.0.3"  # attacker
    H4 = "10.0.0.4"  # video server
    H5 = "10.0.0.5"  # web server

    def __init__(self, *args, **kwargs):
        super(ServiceSlicingController, self).__init__(*args, **kwargs)

        self.datapaths = {} # Memorizza gli switch connessi al controller
        self.flow_stats = {}
        self.blocked_hosts = {} # Registra gli IP attualmente in stato di blocco con il rispettivo orario Unix di sblocco

        self.config = self.load_config()  #imposta le porte 9999 UDP per video e 5001 TCP  per il web , 
                                            #la soglia DOS 8Mbps e il timeout 30s                              
        
        self.video_port = self.config["video_port"]
        self.web_port = self.config["web_port"]
        self.dos_threshold_mbps = self.config["dos_threshold_mbps"]
        self.block_timeout = self.config["block_timeout"]

        #  Supporto dashboard 
        
        self.MEASUREMENT_DPID = 4  #imposta lo switch 4 come unico punto di misurazione per calcolare il throughput
	                           


	# Definiamo i flussi di rete distinte (Video e Web), ciascuno instradato su un percorso fisico distinto
    # e definiamo i flussi che partecipano al rilevamento DoS

        self.tracked_slices = {
            "video_legit": {
                "src": self.H1, "dst": self.H4,
                "port_field": "udp_dst", "port": self.video_port,
                "label": "Video (H1 -> H4)",
                "monitor_dos": True   # partecipa al rilevamento DoS
            },
            "attacker": {
                "src": self.H3, "dst": self.H4,
                "port_field": "udp_dst", "port": self.video_port,
                "label": "Video (H3 -> H4)",
                "monitor_dos": True   # partecipa al rilevamento DoS
            },
            "web": {
                "src": self.H2, "dst": self.H5,
                "port_field": "tcp_dst", "port": self.web_port,
                "label": "Web (H2 -> H5)",
                "monitor_dos": False  # la slice web non è soggetta a soglia DoS
            },
        }

        self.stats_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.json")
        self.slice_last_mbps = {key: 0.0 for key in self.tracked_slices}
        self.slice_history = {key: deque(maxlen=60) for key in self.tracked_slices}
        self.dos_events = deque(maxlen=20)


	# avvia due thread Ryu separati tramite hub.spawn: uno per l'interrogazione 
	#periodica degli switch (monitor) e uno per l'aggiornamento del file JSON 

        self.monitor_thread = hub.spawn(self.monitor)
        self.dashboard_writer_thread = hub.spawn(self.dashboard_writer_loop)

        self.logger.info("Controller SDN Service Slicing avviato")
        self.logger.info("Porta video UDP: %s", self.video_port)
        self.logger.info("Porta web TCP: %s", self.web_port)
        self.logger.info("Soglia DoS: %s Mbps", self.dos_threshold_mbps)

    def load_config(self):
        #se non riesce ad aprire il config usiamo config di default
        default_config = {
            "video_port": 9999,
            "web_port": 5001,
            "dos_threshold_mbps": 8,
            "block_timeout": 30
        }

        try:
            with open("config.json", "r") as file:
                return json.load(file)
        except Exception:
            self.logger.warning("config.json non trovato o non valido, uso valori di default")
            return default_config


    #Gestiamo l'Handshake tra switch e controller
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath #switch connesso
        parser = datapath.ofproto_parser #Il costruttore dei messaggi OpenFlow per lo switch.
        ofproto = datapath.ofproto

        self.datapaths[datapath.id] = datapath

	# L'azione OFPP_CONTROLLER invia il pacchetto al controller 
    # dicendo allo switch se hai un pacchetto che corrisponde con match fai actions questo ha una priorita bassa
    # l'azione dice di inviare il pacchetto al controller cosi lo facciamo gestire al controller (lo usiamo con i pacchetti nuovi)
    # perche se ha registrata una regola di instradamento a priorita piu alta la segue  

        match = parser.OFPMatch()  #match vuoto (cattura qualsiasi pacchetto senza regola specifica)

        actions = [
            parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)
        ]
	
        # installa una la regola con priority=0  
        self.add_flow(datapath, priority=0, match=match, actions=actions)
        self.logger.info("Switch connesso: s%s", datapath.id)

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0, hard_timeout=0):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        instructions = [
            parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)
        ]

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout
        )

        datapath.send_msg(mod)

    def add_drop_flow(self, datapath, match, hard_timeout=0):
        self.add_flow(
            datapath=datapath,
            priority=100,
            match=match,
            actions=[],
            hard_timeout=hard_timeout
        )


    # Smistamento dei pacchetti e Isolamento Slice 
    # Viene eseguita ogni volta che uno switch riceve un pacchetto "nuovo" e lo invia al controller per chiedere istruzioni
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev): 
        
        msg = ev.msg # Estrae l'oggetto messaggio OpenFlow contenuto nell'evento ev
        datapath = msg.datapath
        in_port = msg.match["in_port"] #Legge la porta di ingresso dello switch attraverso la quale il pacchetto è entrato fisicamente

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        # se il pacchetto viene usato per scoperta topologica (LLDP) lo ignoriamo facendo return
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        # caso in cui il protocollo è ARP e inoltra al metodo per la risoluzione indirizzi (handle_arp)
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            self.handle_arp(datapath, pkt, eth, arp_pkt, in_port)
            return

        # caso in cui il protocollo è IP gestisce il pacchetto con handle_ipv4
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt:
            self.handle_ipv4(datapath, pkt, eth, ip_pkt, in_port)
            return
	
    #handle_arp: Gestisce la risoluzione indirizzi ARP
    #Verifica preventivamente se la coppia (src_ip, dst_ip) è autorizzata tramite get_service_by_hosts
    #

    def handle_arp(self, datapath, pkt, eth, arp_pkt, in_port):
        parser = datapath.ofproto_parser
        dpid = datapath.id #id dello switch

        src_ip = arp_pkt.src_ip
        dst_ip = arp_pkt.dst_ip

        # verifica se la comunicazione tra (src_ip, dst_ip) è autorizzata 
        service = self.get_service_by_hosts(src_ip, dst_ip)

        if service is None:
            self.logger.info("ARP bloccato: %s -> %s", src_ip, dst_ip)
            return

        # calcola la porta fisica di uscita dello switch con la funzione get_out_port
        out_port = self.get_out_port(dpid, src_ip, dst_ip, service)

        if out_port is None:
            return

        #costruisce la regola di filtraggio allo switch cosi la prossima volta non lo rimanda al controller
        match = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_ARP,
            arp_spa=src_ip,
            arp_tpa=dst_ip
        )

        actions = [parser.OFPActionOutput(out_port)]
        self.add_flow(datapath, priority=10, match=match, actions=actions)
        self.send_packet_out(datapath, pkt, in_port, out_port)


    # Classifica il servizio, applica il filtraggio per traffico non autorizzato
    # calcola il percorso e installa la regola di forwarding
    def handle_ipv4(self, datapath, pkt, eth, ip_pkt, in_port):
        parser = datapath.ofproto_parser
        dpid = datapath.id

        src_ip = ip_pkt.src
        dst_ip = ip_pkt.dst
        ip_proto = ip_pkt.proto

        service = None
        match_extra = {}
	
       # Distinguiamo i flussi in base alle porte di destinazione per lo slice video e web
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        udp_pkt = pkt.get_protocol(udp.udp)

        # Caso in cui abbiamo slice video UDP porta 9999
        if udp_pkt and udp_pkt.dst_port == self.video_port:
            service = "video"
            match_extra = {
                "ip_proto": 17,
                "udp_dst": self.video_port
            }

        # Caso in cui abbiamo slice web TCP porta 5001
        elif tcp_pkt and tcp_pkt.dst_port == self.web_port:
            service = "web"
            match_extra = {
                "ip_proto": 6,
                "tcp_dst": self.web_port
            }

        else:
            service = self.get_service_by_hosts(src_ip, dst_ip)
            match_extra = {
                "ip_proto": ip_proto
            }

        # se la coppia sorgente destinazione non e autorizzata da nessuna slice scarta il pacchetto
        if service is None:
            self.logger.info("Traffico bloccato: %s -> %s", src_ip, dst_ip)
            return

        if self.is_blocked(src_ip):
            self.logger.warning("Pacchetto scartato: host %s temporaneamente bloccato", src_ip)
            return

        #calcola porta di uscita dello switch
        out_port = self.get_out_port(dpid, src_ip, dst_ip, service)

        if out_port is None:
            return

        match_fields = {
            "eth_type": ether_types.ETH_TYPE_IP,
            "ipv4_src": src_ip,
            "ipv4_dst": dst_ip
        }

        match_fields.update(match_extra)

        match = parser.OFPMatch(**match_fields)
        actions = [parser.OFPActionOutput(out_port)]

        self.add_flow(
            datapath=datapath,
            priority=20,
            match=match,
            actions=actions,
            idle_timeout=20
        )

        self.logger.info(
            "Flow installato su s%s: %s -> %s servizio=%s out_port=%s",
            dpid, src_ip, dst_ip, service, out_port
        )

        self.send_packet_out(datapath, pkt, in_port, out_port)

    # Definiamo la matrice di instradamento e i percorsi
    def get_service_by_hosts(self, src_ip, dst_ip):
        video_pairs = [
            (self.H1, self.H4),
            (self.H4, self.H1),
            (self.H3, self.H4),
            (self.H4, self.H3)
        ]

        web_pairs = [
            (self.H2, self.H5),
            (self.H5, self.H2)
        ]

        if (src_ip, dst_ip) in video_pairs:
            return "video"

        if (src_ip, dst_ip) in web_pairs:
            return "web"

        return None



    def get_out_port(self, dpid, src_ip, dst_ip, service):
        if service == "video":
            return self.video_path(dpid, src_ip, dst_ip)

        if service == "web":
            return self.web_path(dpid, src_ip, dst_ip)

        return None



    
    #Separazione dei percorsi fisici
    def video_path(self, dpid, src_ip, dst_ip):
        # Percorso video: s1 -> s2 -> s4

        if src_ip in [self.H1, self.H3] and dst_ip == self.H4:
            ports = {
                1: 4,  # s1 verso s2    #questi dipendono da come sono stati creati i link nella topologia
                2: 2,  # s2 verso s4
                4: 1   # s4 verso h4
            }
            return ports.get(dpid)

        if src_ip == self.H4 and dst_ip == self.H1:
            ports = {
                4: 3,  # s4 verso s2
                2: 1,  # s2 verso s1
                1: 1   # s1 verso h1
            }
            return ports.get(dpid)

        if src_ip == self.H4 and dst_ip == self.H3:
            ports = {
                4: 3,  # s4 verso s2
                2: 1,  # s2 verso s1
                1: 3   # s1 verso h3
            }
            return ports.get(dpid)

        return None

    def web_path(self, dpid, src_ip, dst_ip):
        # Percorso web: s1 -> s3 -> s4

        if src_ip == self.H2 and dst_ip == self.H5:
            ports = {
                1: 5,  # s1 verso s3
                3: 2,  # s3 verso s4
                4: 2   # s4 verso h5
            }
            return ports.get(dpid)

        if src_ip == self.H5 and dst_ip == self.H2:
            ports = {
                4: 4,  # s4 verso s3
                3: 1,  # s3 verso s1
                1: 2   # s1 verso h2
            }
            return ports.get(dpid)

        return None
   
   # invia il pacchetto iniziale che ha scatenato la PacketIn direttamente verso la porta
   # fisica di uscita identificata
    def send_packet_out(self, datapath, pkt, in_port, out_port):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        actions = [parser.OFPActionOutput(out_port)]

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=pkt.data
        )

        datapath.send_msg(out)

#thread di monitoraggio
    def monitor(self):
        while True:
            #per tutti gli switch
            for datapath in list(self.datapaths.values()):
                self.request_stats(datapath) #invia una richiesta di statistiche

            hub.sleep(5)

#scrittura sulla dashboard 


#loop di aggiornamento dello stato ogni 2 secondi 
    def dashboard_writer_loop(self):
        # Scrive periodicamente lo stato corrente su stats.json, cosi'
        # la dashboard vede anche i momenti in cui non arrivano nuove
        # FlowStatsReply (es. nessun traffico sul flusso video).
        while True:
            self.write_stats()
            hub.sleep(2)

    def write_stats(self):
        now = time.time()

        blocked_hosts_payload = [
            {"ip": ip, "remaining_seconds": round(until - now, 1)}
            for ip, until in self.blocked_hosts.items()
            if until > now
        ]

        slices_payload = {}
        for slice_key, info in self.tracked_slices.items():
            slices_payload[slice_key] = {
                "label": info["label"],
                "src": info["src"],
                "dst": info["dst"],
                "port": info["port"],
                "current_mbps": round(self.slice_last_mbps[slice_key], 3),
                "history": list(self.slice_history[slice_key])
            }

        payload = {
            "timestamp": now,
            "config": {
                "video_port": self.video_port,
                "web_port": self.web_port,
                "dos_threshold_mbps": self.dos_threshold_mbps,
                "block_timeout": self.block_timeout
            },
            "slices": slices_payload,
            "blocked_hosts": blocked_hosts_payload,
            "dos_events": list(self.dos_events),
            "switches_connected": sorted(list(self.datapaths.keys()))
        }

        # Scrittura atomica per evitare che la dashboard legga un file
        try:
            dir_name = os.path.dirname(self.stats_file)
            with tempfile.NamedTemporaryFile(
                "w", dir=dir_name, delete=False, suffix=".tmp"
            ) as tmp_file:
                json.dump(payload, tmp_file)
                tmp_path = tmp_file.name
            os.replace(tmp_path, self.stats_file)
        except Exception as exc:
            self.logger.warning("Impossibile scrivere stats.json: %s", exc)

    def request_stats(self, datapath):
        parser = datapath.ofproto_parser
        req = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req)



    #funzione che riceve le statistiche del traffico dagli switch e calcola il  Mbps
    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        datapath = ev.msg.datapath
        dpid = datapath.id
        now = time.time()

        # Misuriamo solo sullo switch di uscita (s4), dove sia H4 che H5
        # sono collegati: cosi' contiamo ogni pacchetto una volta sola,
        # invece di ricontarlo su ogni switch attraversato lungo il
        # percorso (s1 -> s2/s3 -> s4).
        if dpid != self.MEASUREMENT_DPID:
            return

        for stat in ev.msg.body:
            match = stat.match

            # per ogni statistica controlla sorgente e destinazione se gia l'ha annotato aggiorna solo i Mbps altrimenti crea una nuova statica
            if "ipv4_src" not in match or "ipv4_dst" not in match:
                continue

            src_ip = match["ipv4_src"]
            dst_ip = match["ipv4_dst"]

            # Confrontiamo lo stato corrente con ciascuna delle slice che
            # vogliamo tracciare (video legittimo, web, flusso attaccante).
            for slice_key, info in self.tracked_slices.items():
                if src_ip != info["src"] or dst_ip != info["dst"]:
                    continue

                if info["port_field"] not in match or match[info["port_field"]] != info["port"]:
                    continue

                self._update_slice_mbps(slice_key, info, stat.byte_count, now)
                break  # trovata la slice corrispondente, passa allo stat successivo

    def _update_slice_mbps(self, slice_key, info, byte_count, now):
        old_data = self.flow_stats.get(slice_key)

        if old_data is not None:
            old_bytes, old_time = old_data
            delta_bytes = byte_count - old_bytes
            delta_time = now - old_time

            # vediamo se i byte contati ora sono minori a quelli vecchi 
            if delta_bytes < 0:
                # La regola e' stata reinstallata (idle_timeout scaduto):
                # il contatore riparte da zero, scartiamo questo campione.
                self.flow_stats[slice_key] = (byte_count, now)
                return

            if delta_time > 0:
                mbps = (delta_bytes * 8) / (delta_time * 1_000_000)

                self.logger.info(
                    "Monitor slice '%s': %s -> %s = %.2f Mbps",
                    slice_key, info["src"], info["dst"], mbps
                )

                self.slice_last_mbps[slice_key] = mbps
                self.slice_history[slice_key].append({"t": now, "mbps": round(mbps, 3)})

                # Se i Mbps sono maggiori della soglia blocca l'host
                if info.get("monitor_dos") and mbps > self.dos_threshold_mbps:
                    self.logger.warning(
                        "Possibile DoS rilevato: %s -> %s %.2f Mbps",
                        info["src"], info["dst"], mbps
                    )
                    self.block_host(info, mbps)

        self.flow_stats[slice_key] = (byte_count, now)


    # mitigazione e blocco dei pacchetti
    def block_host(self, info, current_mbps):
        host_ip = info["src"]
        now = time.time()

        if host_ip in self.blocked_hosts:
            if now < self.blocked_hosts[host_ip]:
                return

        self.blocked_hosts[host_ip] = now + self.block_timeout

        #associare il corretto numero di protocollo IP, UDP
        ip_proto = 17 if info["port_field"] == "udp_dst" else 6
	
	# installa le regole di drop per tutti gli switch	
        for datapath in self.datapaths.values():
            parser = datapath.ofproto_parser  # costruttore degli oggetti e dei messaggi OpenFlow

            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_src=host_ip,
                ipv4_dst=info["dst"],
                ip_proto=ip_proto,
                **{info["port_field"]: info["port"]}
            )

            self.add_drop_flow(
                datapath=datapath,
                match=match,
                hard_timeout=self.block_timeout
            )

        self.logger.warning(
            "Host %s bloccato per %s secondi (traffico anomalo verso %s)",
            host_ip, self.block_timeout, info["dst"]
        )

        self.dos_events.append({
            "t": now,
            "attacker": host_ip,
            "mbps": round(current_mbps, 3),
            "block_timeout": self.block_timeout
        })
        self.write_stats()

    def is_blocked(self, ip):
        if ip not in self.blocked_hosts:
            return False

        if time.time() > self.blocked_hosts[ip]:
            del self.blocked_hosts[ip]
            return False

        return True
