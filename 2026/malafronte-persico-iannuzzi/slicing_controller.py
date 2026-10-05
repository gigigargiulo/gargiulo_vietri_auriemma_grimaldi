import os
import time
import json

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, udp, ether_types, in_proto
from ryu.lib import hub                # scheduler cooperativo di Ryu, usato per il monitor periodico

MODE = os.environ.get('SLICING_MODE', 'topology').lower() # modalita' scelta all'avvio: topology o service
DYNAMIC = os.environ.get('DYNAMIC', '0') == '1'           # politica dinamica, valida solo in modalita' service

VIDEO_UDP_PORT = 9999  # porta UDP di destinazione che identifica il traffico video

SLICE_OF_MAC = {    # appartenenza degli host agli slice (modalita' topology)
    '00:00:00:00:00:01': 'UPPER',   # h1
    '00:00:00:00:00:03': 'UPPER',   # h3
    '00:00:00:00:00:02': 'LOWER',   # h2
    '00:00:00:00:00:04': 'LOWER',   # h4
}

HOST_LOCATION = {                 # posizione degli host: MAC -> (dpid dello switch, porta)
    '00:00:00:00:00:01': (1, 1),  # h1
    '00:00:00:00:00:02': (1, 2),  # h2
    '00:00:00:00:00:03': (4, 1),  # h3
    '00:00:00:00:00:04': (4, 2),  # h4
}

SLICE_PATH = {  # topology: slice -> switch -> {porta di ingresso: porta di uscita}
    'UPPER': {
        1: {1: 3, 3: 1},      # s1 tra h1 ed s2
        2: {1: 2, 2: 1},      # s2 tra s1 ed s4
        4: {1: 3, 3: 1},      # s4 tra h3 ed s2
    },
    'LOWER': {
        1: {2: 4, 4: 2},      # s1 tra h2 ed s3
        3: {1: 2, 2: 1},      # s3 tra s1 ed s4
        4: {2: 4, 4: 2},      # s4 tra h4 ed s3
    },
}

EDGE_UPLINK = {                   # service: porta verso il core, per slice e per switch di accesso
    'UPPER': {1: 3, 4: 3},        # via s2
    'LOWER': {1: 4, 4: 4},        # via s3
}

TRANSIT = {                       # service: attraversamento degli switch di core
    2: {1: 2, 2: 1},              # s2 (UPPER)
    3: {1: 2, 2: 1},              # s3 (LOWER)
}

BCAST_TREE = {                    # service: albero del broadcast, senza s2 per spezzare l'anello
    1: [1, 2, 4],                 # s1: h1, h2, s3
    3: [1, 2],                    # s3: s1, s4
    4: [1, 2, 4],                 # s4: h3, h4, s3
}

IDLE_TIMEOUT = 30                 # secondi di inattivita' dopo i quali una regola scade
PRIO_VIDEO = 20                   # regole del traffico video
PRIO_FORWARD = 10                 # regole di inoltro ordinario
PRIO_DROP = 5                     # regole di scarto
PRIO_MISS = 0                     # table-miss: pacchetti senza regola, inviati al controller

# --- Monitor e dashboard ---
STATS_INTERVAL = 1                # periodo di interrogazione di s1, in secondi
STATS_FILE = 'stats.json'         # file letto dalla dashboard
HISTORY_LEN = 60                  # campioni conservati (un minuto)
MONITORED = {                     # slice -> (dpid, porta): le due uscite di s1 verso il core
    'UPPER': (1, 3),
    'LOWER': (1, 4),
}

# --- Slicing dinamico: prestito dello slice upper al traffico best effort ---
BE_SRC = '00:00:00:00:00:02'      # h2: primo estremo della coppia a cui si presta lo slice
BE_DST = '00:00:00:00:00:04'      # h4: secondo estremo (il prestito vale in entrambi i versi)
SOGLIA_PRESTITO = 1.0             # Mbps: sotto questo valore di traffico video lo slice viene prestato
SOGLIA_RIENTRO = 3.0              # Mbps: sopra questo valore il prestito viene revocato
MIN_DT = 1.5                      # s: intervallo minimo fra due campioni per una misura affidabile
PRIO_DYNAMIC = 15                 # sopra l'inoltro ordinario (10), sotto il video (20)

class NetworkSlicing(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(NetworkSlicing, self).__init__(*args, **kwargs)
        self.mac_to_port = {}                    # dpid -> {mac: porta}, solo diagnostica
        self.recent_flows = {}                   # (dpid, priorita', match) -> istante di installazione
        if MODE not in ('topology', 'service'):
            raise ValueError("SLICING_MODE deve essere 'topology' o 'service'")
        self.logger.info('=== MODALITA\': %s ===', MODE.upper())
        self.dynamic_on = DYNAMIC and MODE == 'service'   # la politica dinamica si basa sul traffico video
        if DYNAMIC and not self.dynamic_on:
            self.logger.warning('[DYNAMIC] ignorato: richiede SLICING_MODE=service')
        elif self.dynamic_on:
            self.logger.info('=== DYNAMIC SLICING: ATTIVO ===')
        self.datapaths = {}                      # dpid -> datapath degli switch connessi
        self.prev_bytes = {}                     # (dpid, porta) -> (tx_bytes, rx_bytes, istante) del campione precedente
        self.port_rate = {}                      # (dpid, porta) -> (tx Mbps, rx Mbps)
        self.history = []                        # ultimi HISTORY_LEN campioni scritti in STATS_FILE
        self.video_mbps = 0.0                    # rate del traffico video misurato su s1
        self.video_bytes = None                  # byte video al campione precedente (None: nessun campione)
        self.video_time = time.time()            # istante del campione precedente
        self.prestito = False                    # True quando h2 <-> h4 usa lo slice upper
        self.monitor_thread = hub.spawn(self._monitor)   # avvia il monitor in parallelo agli handler

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):       # eseguito alla connessione di ogni switch
        datapath = ev.msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        match = parser.OFPMatch()                # table-miss: qualsiasi pacchetto, per intero, al controller
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(datapath, PRIO_MISS, match, actions)
        self.logger.info('[SWITCH] s%s connesso', datapath.id)
        self.datapaths[datapath.id] = datapath           # riferimento usato dal monitor e dal prestito
        if self.prestito and datapath.id in (1, 4):      # switch riconnesso con tabella vuota a prestito attivo
            self._applica_prestito()                     # reinstalla la deviazione

    def _monitor(self):                          # ciclo periodico: chiede a s1 le statistiche
        while True:
            dp = self.datapaths.get(1)           # s1 e' un estremo di entrambi i percorsi core
            if dp is not None:
                parser = dp.ofproto_parser
                ofproto = dp.ofproto
                req_porte = parser.OFPPortStatsRequest(dp, 0, ofproto.OFPP_ANY)   # contatori di tutte le porte
                dp.send_msg(req_porte)
                filtro = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,        # solo le regole del traffico video
                                         ip_proto=in_proto.IPPROTO_UDP,
                                         udp_dst=VIDEO_UDP_PORT)
                req_flussi = parser.OFPFlowStatsRequest(dp, 0, ofproto.OFPTT_ALL,
                                                        ofproto.OFPP_ANY, ofproto.OFPG_ANY,
                                                        0, 0, filtro)             # cookie e cookie_mask a zero: nessun filtro
                dp.send_msg(req_flussi)
            hub.sleep(STATS_INTERVAL)            # cede il controllo agli handler (time.sleep bloccherebbe il controller)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):      # i contatori sono cumulativi: il rate e' la differenza fra due campioni
        dpid = ev.msg.datapath.id
        for stat in ev.msg.body:
            key = (dpid, stat.port_no)
            t = stat.duration_sec + stat.duration_nsec / 1e9   # tempo misurato dallo switch, non dal controller
            prev = self.prev_bytes.get(key)
            if prev is not None:                 # il primo campione serve solo da riferimento
                dt = t - prev[2]
                d_tx = stat.tx_bytes - prev[0]
                d_rx = stat.rx_bytes - prev[1]
                if dt > 0 and d_tx >= 0 and d_rx >= 0:         # scarta i campioni con contatori azzerati
                    self.port_rate[key] = (d_tx * 8 / dt / 1e6, d_rx * 8 / dt / 1e6)   # byte -> Mbps
            self.prev_bytes[key] = (stat.tx_bytes, stat.rx_bytes, t)
        self._write_stats()

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):      # misura il video sulle sue regole: sul link passa anche il traffico prestato
        totale = 0                               # byte contati da tutte le regole video di s1
        for stat in ev.msg.body:
            totale += stat.byte_count
        now = time.time()
        if self.video_bytes is None:             # primo campione: solo riferimento
            self.video_bytes = totale
            self.video_time = now
            return
        dt = now - self.video_time
        if dt < MIN_DT:                          # intervallo troppo breve: misura poco affidabile
            return
        delta = totale - self.video_bytes        # negativo se le regole video sono scadute e i contatori ripartiti
        self.video_mbps = max(0.0, delta * 8 / dt / 1e6)
        self.video_bytes = totale
        self.video_time = now
        if self.dynamic_on:
            self._decidi_prestito()

    def _write_stats(self):                      # aggiorna il file letto dalla dashboard
        sample = {'t': round(time.time(), 1), 'mode': MODE,
                  'dynamic': self.dynamic_on, 'prestito': self.prestito,
                  'video': round(self.video_mbps, 3)}
        for name, key in MONITORED.items():
            tx, rx = self.port_rate.get(key, (0.0, 0.0))       # zero finche' non c'e' una misura
            sample[name] = {'tx': round(tx, 3), 'rx': round(rx, 3)}
        self.history.append(sample)
        self.history = self.history[-HISTORY_LEN:]             # conserva solo gli ultimi HISTORY_LEN campioni
        tmp = STATS_FILE + '.tmp'
        try:                                     # un errore di scrittura non deve fermare il controller
            with open(tmp, 'w') as f:
                json.dump(self.history, f)
            os.replace(tmp, STATS_FILE)          # sostituzione atomica: la dashboard non legge mai un file incompleto
        except OSError as e:
            self.logger.warning('[STATS] scrittura fallita: %s', e)

    def _decidi_prestito(self):                  # due soglie distinte: fra le due lo stato non cambia
        if not self.prestito and self.video_mbps < SOGLIA_PRESTITO:
            self.prestito = True
            self._applica_prestito()
            self.logger.info('[DYNAMIC] video a %.2f Mbps: presto lo slice upper a h2 <-> h4', self.video_mbps)
        elif self.prestito and self.video_mbps > SOGLIA_RIENTRO:
            self.prestito = False
            self._applica_prestito()
            self.logger.info('[DYNAMIC] video a %.2f Mbps: h2 <-> h4 torna sullo slice lower', self.video_mbps)

    def _applica_prestito(self):                 # una regola per verso: su s1 per h2 -> h4, su s4 per h4 -> h2
        for dpid, src, dst in ((1, BE_SRC, BE_DST), (4, BE_DST, BE_SRC)):
            dp = self.datapaths.get(dpid)
            if dp is None:                       # switch non connesso
                continue
            if self.prestito:
                self._regola_dynamic(dp, src, dst)     # devia la coppia sullo slice upper
            else:
                self._cancella_dynamic(dp, src, dst)   # il traffico torna sul lower con le regole ordinarie

    def _regola_dynamic(self, datapath, src, dst):     # match sui soli MAC: copre l'inoltro ordinario (10), non il video (20)
        parser = datapath.ofproto_parser
        out_port = EDGE_UPLINK['UPPER'][datapath.id]   # porta verso s2
        actions = [parser.OFPActionOutput(out_port)]
        match = parser.OFPMatch(eth_src=src, eth_dst=dst)
        self._add_flow(datapath, PRIO_DYNAMIC, match, actions)   # senza idle_timeout: resta finche' non viene revocata
        self.logger.info('[DYNAMIC] s%s: %s -> %s deviato su porta %s', datapath.id, src, dst, out_port)

    def _cancella_dynamic(self, datapath, src, dst):   # DELETE_STRICT: rimuove solo la regola con questo match e questa priorita'
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        match = parser.OFPMatch(eth_src=src, eth_dst=dst)
        mod = parser.OFPFlowMod(datapath=datapath, command=ofproto.OFPFC_DELETE_STRICT,
                                priority=PRIO_DYNAMIC, match=match,
                                out_port=ofproto.OFPP_ANY, out_group=ofproto.OFPG_ANY)  # ANY: nessun filtro sulla porta di uscita
        datapath.send_msg(mod)
        self.logger.info('[DYNAMIC] s%s: rimossa la deviazione %s -> %s', datapath.id, src, dst)

    def _add_flow(self, datapath, priority, match, actions, idle=0):  # installa una flow entry sullo switch
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        # lista di azioni vuota => nessuna istruzione => il pacchetto e' scartato
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)] if actions else []
        kwargs = dict(datapath=datapath, priority=priority, match=match, instructions=inst, idle_timeout=idle)
        mod = parser.OFPFlowMod(**kwargs)
        datapath.send_msg(mod)

    def _send_packet(self, msg, actions):       # restituisce allo switch il pacchetto ricevuto, con le azioni da applicare
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        data = msg.data                     # la table-miss invia sempre il pacchetto completo
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=ofproto.OFP_NO_BUFFER, in_port=msg.match['in_port'], actions=actions, data=data)
        datapath.send_msg(out)

    def _already_installed(self, key):         # evita di reinstallare una regola richiesta piu' volte in pochi istanti
        now = time.time()
        last = self.recent_flows.get(key)
        if last is not None and now - last < IDLE_TIMEOUT:         # regola installata di recente
            return True
        self.recent_flows[key] = now
        if len(self.recent_flows) > 1000:      # rimuove le voci scadute
            flows_puliti = {}
            for k, t in self.recent_flows.items():
                if now - t < IDLE_TIMEOUT:
                    flows_puliti[k] = t
            self.recent_flows = flows_puliti
        return False

    @staticmethod
    def _is_bum(mac):                          # True per broadcast e multicast, False per unicast
        return bool(int(mac.split(':')[0], 16) & 1)  # bit meno significativo del primo ottetto: bit Individual/Group

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):                                  # gestisce i pacchetti per cui lo switch non ha una regola
        msg = ev.msg
        datapath = msg.datapath                                       # switch che ha inviato il pacchetto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        in_port = msg.match['in_port']                                # porta da cui il pacchetto e' entrato nello switch

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP: # ignora pacchetti non Ethernet e LLDP
            return

        src = eth.src
        dst = eth.dst

        if dpid not in self.mac_to_port:
            self.mac_to_port[dpid] = {}
        self.mac_to_port[dpid][src] = in_port

        if src not in HOST_LOCATION:                                  # sorgente non censita: regola di scarto
            match = parser.OFPMatch(eth_src=src)
            if not self._already_installed((dpid, PRIO_DROP, str(match))):
                self.logger.info('[DROP] s%s: sorgente sconosciuta %s', dpid, src)
                self._add_flow(datapath, PRIO_DROP, match, [], idle=IDLE_TIMEOUT)
            return                                                   # anche il pacchetto corrente viene scartato

        if MODE == 'topology':
            self._handle_topology(msg, pkt, eth, dpid, in_port, src, dst)
        else:
            self._handle_service(msg, pkt, eth, dpid, in_port, src, dst)

    def _handle_topology(self, msg, pkt, eth, dpid, in_port, src, dst):   # topology: isolamento fra slice
        datapath = msg.datapath
        parser = datapath.ofproto_parser

        slice_name = SLICE_OF_MAC.get(src)                                # lo slice e' quello del mittente
        if slice_name is None:
            return

        bum = self._is_bum(dst)

        # Isolamento: unicast verso un host di un altro slice -> scarto
        if not bum and SLICE_OF_MAC.get(dst) != slice_name:
            match = parser.OFPMatch(eth_src=src, eth_dst=dst)
            if not self._already_installed((dpid, PRIO_DROP, str(match))):
                self.logger.info('[DROP] s%s: %s (%s) -> %s : slice diversi', dpid, src, slice_name, dst)
                self._add_flow(datapath, PRIO_DROP, match, [], idle=IDLE_TIMEOUT)
            return

        out_port = SLICE_PATH[slice_name].get(dpid, {}).get(in_port)               # porta di uscita lungo il percorso dello slice
        if out_port is None:
            return                                                                 # porta estranea al percorso dello slice

        actions = [parser.OFPActionOutput(out_port)]

        if bum:                                   # broadcast: solo inoltro, nessuna regola installata
            self._send_packet(msg, actions)
            return

        match = parser.OFPMatch(in_port=in_port, eth_src=src, eth_dst=dst)
        if self._already_installed((dpid, PRIO_FORWARD, str(match))):
            self._send_packet(msg, actions)                                        # regola gia' installata: solo inoltro
            return

        self.logger.info('[FLOW] s%s [%s] %s -> %s : porta %s -> %s', dpid, slice_name, src, dst, in_port, out_port)

        self._add_flow(datapath, PRIO_FORWARD, match, actions, idle=IDLE_TIMEOUT)
        self._send_packet(msg, actions)

    def _handle_service(self, msg, pkt, eth, dpid, in_port, src, dst):             # service: percorso scelto in base al servizio
        datapath = msg.datapath
        parser = datapath.ofproto_parser

        # Broadcast e multicast: replica sulle porte dell'albero, esclusa quella di ingresso
        if self._is_bum(dst):
            ports = []
            porte_sicure = BCAST_TREE.get(dpid, [])
            for p in porte_sicure:
                if p != in_port:
                    ports.append(p)
            if ports:
                azioni = []
                for p in ports:
                    comando = parser.OFPActionOutput(p)
                    azioni.append(comando)
                self._send_packet(msg, azioni)                                     # una copia del pacchetto per ogni porta
            return

        if dst not in HOST_LOCATION:                                               # destinazione non censita
            return

        # Classificazione: e' video il traffico UDP diretto alla porta VIDEO_UDP_PORT
        is_video = False
        udp_hdr = None
        ip4 = pkt.get_protocol(ipv4.ipv4)
        if ip4 is not None and ip4.proto == in_proto.IPPROTO_UDP:
            udp_hdr = pkt.get_protocol(udp.udp)
            if udp_hdr is not None and udp_hdr.dst_port == VIDEO_UDP_PORT:
                is_video = True
        slice_name = 'UPPER' if is_video else 'LOWER'                              # politica di base: video su upper, il resto su lower

        # Calcolo della porta di uscita
        dst_dpid, dst_port = HOST_LOCATION[dst]
        if dpid == dst_dpid:                                                       # destinatario collegato a questo switch
            out_port = dst_port
        elif dpid in EDGE_UPLINK[slice_name]:                                      # switch di accesso: uscita verso il core dello slice scelto
            out_port = EDGE_UPLINK[slice_name][dpid]
        elif dpid in TRANSIT:                                                      # switch di core: attraversamento
            out_port = TRANSIT[dpid].get(in_port)
        else:
            out_port = None

        if out_port is None:
            return

        actions = [parser.OFPActionOutput(out_port)]

        # Costruzione del match: specifico quanto la classe di traffico
        if ip4 is None:                                                            # non IPv4 (in pratica ARP): match fino all'EtherType
            match = parser.OFPMatch(in_port=in_port, eth_src=src, eth_dst=dst, eth_type=eth.ethertype)
            priority = PRIO_FORWARD
        elif ip4.proto == in_proto.IPPROTO_UDP:                                    # UDP: match fino alla porta di destinazione
            match = parser.OFPMatch(in_port=in_port, eth_src=src, eth_dst=dst, eth_type=ether_types.ETH_TYPE_IP, ip_proto=in_proto.IPPROTO_UDP, udp_dst=udp_hdr.dst_port)
            priority = PRIO_VIDEO if is_video else PRIO_FORWARD
        else:                                                                      # IPv4 non UDP (ICMP, TCP): match fino al protocollo
            match = parser.OFPMatch(in_port=in_port, eth_src=src, eth_dst=dst, eth_type=ether_types.ETH_TYPE_IP, ip_proto=ip4.proto)
            priority = PRIO_FORWARD

        if self._already_installed((dpid, priority, str(match))):
            self._send_packet(msg, actions)                                        # regola gia' installata: solo inoltro
            return

        self.logger.info('[FLOW] s%s [%s%s] %s -> %s : porta %s -> %s', dpid, slice_name, ' VIDEO' if is_video else '', src, dst, in_port, out_port)

        self._add_flow(datapath, priority, match, actions, idle=IDLE_TIMEOUT)      # installa la regola
        self._send_packet(msg, actions)                                            # e restituisce il pacchetto allo switch perche' lo inoltri