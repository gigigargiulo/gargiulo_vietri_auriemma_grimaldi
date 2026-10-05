
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import  CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ipv4

class QoSSwitch(app_manager.RyuApp):
	#dichiariamo la versione di OpenFlow
	OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

	def __init__(self, *args, **kwargs):
		super(QoSSwitch,self).__init__(*args,**kwargs)
		self.mac_to_port = {} #dizionario usato per il forwarding

	#funzione chiamata automaticamente quando uno switch si collega al controller 
	@set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
	def switch_features_handler(self, ev):
		datapath = ev.msg.datapath
		ofproto  = datapath.ofproto
		parser = datapath.ofproto_parser

		self.logger.info("Switch connesso:  %s", datapath.id) #log connection

		match = parser.OFPMatch()
		actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
		
		self.add_flow(datapath, 0, match, actions)

	def add_flow(self, datapath, priority, match, actions):
		ofproto = datapath.ofproto
		parser = datapath.ofproto_parser
		
		inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
		
		mod = parser.OFPFlowMod(datapath = datapath, priority = priority, match=match, instructions = inst)
		
		datapath.send_msg(mod)


	@set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
	def packet_in_handler(self, ev):
		msg = ev.msg
		self.logger.info("Packet-In ricevuto!")
		datapath = msg.datapath
		ofproto = datapath.ofproto
		parser = datapath.ofproto_parser
		in_port = msg.match['in_port'] #porta fisica da cui arriva il pacchetto

		pkt = packet.Packet(msg.data) 
		eth = pkt.get_protocol(ethernet.ethernet) #estrazione header ethernet per leggere indirizzi MAC

		dst = eth.dst
		src = eth.src
		dpid = datapath.id
		self.mac_to_port.setdefault(dpid, {})

		self.mac_to_port[dpid][src] = in_port

		if dst in self.mac_to_port[dpid]:
			out_port = self.mac_to_port[dpid][dst] # se conosciamo il destinatario 
		else: 
			out_port = ofproto.OFPP_FLOOD #altrimenti: flooding

		actions = [parser.OFPActionOutput(out_port)]

		if out_port != ofproto.OFPP_FLOOD:
			ip_pkt = pkt.get_protocol(ipv4.ipv4)

			if ip_pkt is not None:
				if ip_pkt.proto == 17:
					priority = 100
					match = parser.OFPMatch(eth_type = 0x0800, ip_proto = 17, in_port = in_port)
					self.add_flow(datapath, priority, match, actions)
					self.logger.info("Regola UDP installata!")
				elif ip_pkt.proto == 6:
					priority = 10
					match = parser.OFPMatch(eth_type = 0x0800, ip_proto = 6, in_port = in_port)
					self.add_flow(datapath, priority, match, actions)
					self.logger.info("Regola TCP installata!")

		out = parser.OFPPacketOut(
			datapath = datapath, buffer_id = msg.buffer_id, in_port = in_port, actions = actions, data = msg.data)
		datapath.send_msg(out)
		
