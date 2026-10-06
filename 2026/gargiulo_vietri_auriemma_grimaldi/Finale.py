from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types

import time   # SERVE A MISURARE IL TEMPO TRA 2 LETTURE NELLO SWITCH
from ryu.lib import hub    # IMPORTA il modulo HUB, permette di avviare attività in background (chiedo informazioni al controller senza interromperne il funzionamento)


class SimpleSwitch13(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SimpleSwitch13, self).__init__(*args, **kwargs)
        self.mac_to_port = {}

        self.s3 = None         # self si riferisce all'istanza attuale della classe, è il DATAPATH OPENFLOW di s3, oggetto con cui Ryu comunica con SWITCH
        self.misurazione_precedente = None  # contatori openflow sono cumulativi, quindi devo conservare il valore precedente per fare la sottrazione!
        self.tempo_precedente = None  # conserva l'istante della misurazione precedente
        
        # NUOVE VARIABILI PER LA LOGICA A DUE FASI
        self.stato_attacco = 0          # 0 = Normale, 1 = Limitato, 2 = Bloccato
        self.tempo_inizio_limite = None # Salva l'istante in cui inizia la fase 1

        self.thread_monitor = hub.spawn(self.metodomonitor) # avvia monitoraggio in background, del metodo monitor realizzato sotto

    def metodomonitor(self):
       while True:                                # mantiene monitor attivo per tutta la durata del controller
        if self.s3 is not None:                   # invia la richiesta solo dopo che s3 si è collegato
            parser = self.s3.ofproto_parser       # serve a creare messaggi OpenFlow da inviare allo switch, serve a ryu per domandare a s3 le statistiche delle porte
            ofproto = self.s3.ofproto             # Prende le costanti Openflow della versione utilizzata, usato nella riga successiva
            richiesta = parser.OFPPortStatsRequest(self.s3, 0, ofproto.OFPP_ANY)  # è la richiesta per ottenere le statistiche delle porte dello switch, OFPP_ANY indica che vogliamo le statistiche di tutte le porte
            self.s3.send_msg(richiesta)
        hub.sleep(2)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)   # esegue questo metodo solo quando uno switch invia una risposta contenente le statistiche delle porte, associa il metodo ad un evento
    def _ricevi_statistiche_porte(self, ev):     # è il metodo che esegue quando rileva l'evento (ricezione informazioni)
      datapath = ev.msg.datapath

      if datapath.id != 3:     # ignora se le risposte provengono da altri switch che non sono s3
        return

      statistiche = {                # crea un dizionario
        stat.port_no: stat           # recupera il numero della porta, utilizza numero della porta come chiave e l'intero oggetto statistico come valore
        for stat in ev.msg.body      # scorre tutte le statistiche di ogni porta
        }

      porte_necessarie = (3, 4, 5, 6)       # porte richieste (è una tupla), 3=h6, 4=h7, 5=h8, 6=h5

      tempo_attuale = time.monotonic()     # memorizza l'istante di memorizzazione
 
      byte_attuali = {                        # crea dizionario contenente i valori cumulativi
        3: statistiche[3].rx_bytes,
        4: statistiche[4].rx_bytes,
        5: statistiche[5].rx_bytes,
        6: statistiche[6].tx_bytes
        }
      
      if self.misurazione_precedente is None:     # La prima misurazione serve come punto di partenza
        self.misurazione_precedente = byte_attuali
        self.tempo_precedente = tempo_attuale
        return    

      intervallo = tempo_attuale - self.tempo_precedente    # Tempo trascorso tra le due misurazioni, se minore di 0 (prima misurazione), esco
      if intervallo <= 0:
         return

      velocita = {}

      for porta in porte_necessarie:
       differenza_byte = (byte_attuali[porta] - self.misurazione_precedente[porta])
       velocita[porta] = max(0, differenza_byte * 8 / intervallo / 1_000_000)  # formula conversione Mbit/s (differenza_byte*8 converte in bit, /1_000_000 per i Mbit)

      self.logger.info(                                       # Mostra le velocità nel terminale
         'h6=%.2f | h7=%.2f | h8=%.2f | verso h5=%.2f Mbit/s',
           velocita[3],
           velocita[4],
           velocita[5],
           velocita[6]
         )
         
#----------------------RILEVAZIONE DDOS (A DUE FASI)-------------------------------------------------------------------------------
  
      traffico_attaccanti = (velocita[3] + velocita[4] + velocita[5])   # Somma il traffico ricevuto da h6, h7 e h8
      traffico_vittima = velocita[6]     # Traffico trasmesso da s3 verso h5

      soglia_vittima = 80    # Soglia pari all'80% del collegamento da 100 Mbit/s

      # STATO 0: Rete in condizioni normali
      if self.stato_attacco == 0:
          if traffico_attaccanti > 100 and traffico_vittima > soglia_vittima:
              self.logger.warning('Fase 1: DDoS rilevato! Applico limite di banda a 10 Mbit/s.')
              self._limita_attacco(datapath)
              self.stato_attacco = 1
              self.tempo_inizio_limite = tempo_attuale # Faccio partire il timer

      # STATO 1: Traffico attualmente limitato dal Meter
      elif self.stato_attacco == 1:
          # Il traffico verso la vittima è falsato dal limite, quindi verifico solo l'ingresso
          if traffico_attaccanti > 100:
              secondi_trascorsi = tempo_attuale - self.tempo_inizio_limite
              self.logger.info(f'Attacco in corso da {secondi_trascorsi:.1f} secondi...')
              
              if secondi_trascorsi >= 10:
                  self.logger.error('Fase 2: Attacco persistente per oltre 10s. Applico DROP totale!')
                  self._blocca_attacco(datapath)
                  self.stato_attacco = 2 # Passo allo stato bloccato
          else:
              # Se il traffico in ingresso scende, l'attacco è finito prima dei 10s
              self.logger.info('Attacco terminato durante la fase di limite. Ritorno normale.')
              self.stato_attacco = 0
              # (Opzionale: qui si potrebbe rimuovere la regola del limite se si vuole ripristinare del tutto la rete)

#----------------------------------------------------------------------------------------------------------------------------------

      self.misurazione_precedente = byte_attuali        # Il campione attuale diventa il precedente
      self.tempo_precedente = tempo_attuale


    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath

        if datapath.id == 3:      # Voglio monitorare lo switch s3, per cui verifico se DPID è 3, se è verificato eseguo il codice sotto
         self.s3 = datapath       # salvo il datapath dello switch nella variabile s3
    
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match,
                                    instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        if ev.msg.msg_len < ev.msg.total_len:
            self.logger.debug("packet truncated: only %s of %s bytes",
                              ev.msg.msg_len, ev.msg.total_len)
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        dst = eth.dst
        src = eth.src

        dpid = format(datapath.id, "d").zfill(16)
        self.mac_to_port.setdefault(dpid, {})

        self.logger.info("packet in %s %s %s %s", dpid, src, dst, in_port)

        self.mac_to_port[dpid][src] = in_port

        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

    
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
           
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 1, match, actions, msg.buffer_id)
                return
            else:
                self.add_flow(datapath, 1, match, actions)
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

#------------------------------MITIGAZIONE (BLOCCO TOTALE)----------------------------------------------------------------
    def _blocca_attacco(self, datapath):
      parser = datapath.ofproto_parser

      for porta in (3, 4, 5):
        corrispondenza = parser.OFPMatch(        # seleziona pacchetti con queste caratteristiche
            in_port=porta,                       # ha provenienza h6, h7, h8
            eth_type=0x0800,                     # pacchetto IPv4
            ip_proto=17,                         # protocollo UDP
            ipv4_dst='10.0.0.5'                  # la destinazione é h5
        )

        regola_blocco = parser.OFPFlowMod(    # OFPFlowMod crea un messaggio per aggiungere o modificare regola nello switch
            datapath=datapath,     # indico su quale switch installo regola
            priority=100,          # assegno alla regola una priorità pari a 100 (le regole normali hanno 1, questa "vince" anche sul limite)
            match=corrispondenza,  # indica a quali pacchetti si applica la regola
            instructions=[]        # indica cosa deve fare con i pacchetti selezionati, non c'è nulla dunque DROP!
        )

        datapath.send_msg(regola_blocco)   # invio la regola a s3!

#-------------------------------LIMITA ATTACCO (RATE LIMITING)----------------------------------------------------   
    def _limita_attacco(self, datapath):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        # 1. Configuro un Meter che taglia ("DROP") il traffico che supera i 10.000 kbps (10 Mbit/s)
        bands = [parser.OFPMeterBandDrop(rate=10000)]
        meter_mod = parser.OFPMeterMod(
            datapath=datapath,
            command=ofproto.OFPMC_ADD,
            flags=ofproto.OFPMF_KBPS,
            meter_id=1,
            bands=bands
        )
        datapath.send_msg(meter_mod) # Invio la creazione del meter allo switch

        # 2. Creo una regola che intercetta i pacchetti e li passa al Meter
        for porta in (3, 4, 5):
            corrispondenza = parser.OFPMatch(
                in_port=porta, eth_type=0x0800, ip_proto=17, ipv4_dst='10.0.0.5'
            )
            
            # L'azione in questo caso è: processa col meter 1, poi invia verso h5 (porta 6)
            azioni = [parser.OFPActionOutput(6)]
            istruzioni = [
                parser.OFPInstructionMeter(1), # Associa il meter ID 1
                parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, azioni)
            ]
            
            regola_limite = parser.OFPFlowMod(
                datapath=datapath,
                priority=50, # Priorità 50 (minore del DROP totale che è 100, maggiore del normale che è 1)
                match=corrispondenza,
                instructions=istruzioni
            )
            datapath.send_msg(regola_limite)



