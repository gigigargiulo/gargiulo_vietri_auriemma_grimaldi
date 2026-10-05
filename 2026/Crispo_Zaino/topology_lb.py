#!/usr/bin/env python3
r"""
DynLoadBalancer - Mininet Network Topology
Corso di Network and Cloud Infrastructures - Prof. Giorgio Ventre - a.a. 2025/2026

Architettura della Topologia Emulata:
           [ Client Cluster ]
            h1   h2   h3
             \   |   /
              \  |  /
               [ S1 ] (Edge Ingress)
              /      \
    (Path A) /        \ (Path B)
      [ S2 ]            [ S3 ]
        |                 |
      [ S4 ]            [ S5 ]
              \        /
               \      /
                [ S6 ] (Edge Egress)
               /  |  \
             srv1 srv2 srv3
           [ Server Cluster ]

Caratteristiche Canali Core Asimmetrici:
- Path A (S1 - S2 - S4 - S6): BW = 20 Mbps, Delay = 5 ms per link (Canale ad alta capacità ma latenza maggiore)
- Path B (S1 - S3 - S5 - S6): BW = 10 Mbps, Delay = 1 ms per link (Canale a bassa latenza ma capacità ridotta)
"""

import time
import socket
from mininet.log import setLogLevel, info
from mininet.net import Mininet, CLI
from mininet.node import OVSKernelSwitch, RemoteController
from mininet.link import TCLink
from mininet.clean import cleanup


class Environment(object):
    """
    Ambiente Mininet imperativo per l'emulazione della rete multi-path SDN.
    Gestisce il ciclo di vita dell'infrastruttura virtuale: creazione di switch OVS,
    collegamento dei client/server, emulazione delle metriche fisiche tramite Linux TC,
    connessione al controller Ryu remoto ed esponimento della console CLI.
    """

    def __init__(self, cport=6633):
        # ----------------------------------------------------------------------
        # 0. PULIZIA PREVENTIVA AUTOMATICA (PREVIENE 'RTNETLINK File exists')
        # ----------------------------------------------------------------------
        # Se una sessione Mininet precedente è stata interrotta bruscamente (es. Ctrl+C),
        # le interfacce virtuali veth (es. s1-eth4) rimangono registrate nel kernel Linux.
        # cleanup() ripulisce automaticamente bridge OVS e namespace residui.
        try:
            cleanup()
        except Exception:
            pass
        # ----------------------------------------------------------------------
        # 1. RILEVAMENTO AUTOMATICO PORTA DEL CONTROLLER (6653 vs 6633)
        # ----------------------------------------------------------------------
        # Ryu su Ubuntu 22.04 ascolta di default sulla porta ufficiale RFC 7394 (6653).
        # Lo script esegue un test TCP non bloccante con socket.connect_ex per rilevare
        # se Ryu è attivo su 6653 o 6633, evitando fallimenti di connessione degli switch.
        def is_port_open(p):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                return s.connect_ex(('127.0.0.1', p)) == 0

        actual_port = cport
        if is_port_open(6653) and not is_port_open(6633):
            actual_port = 6653
            info("*** Rilevato controller Ryu attivo su porta 6653! Uso porta 6653\n")
        elif is_port_open(6633):
            actual_port = 6633
            info("*** Rilevato controller Ryu attivo su porta 6633! Uso porta 6633\n")
        else:
            info(f"*** ATTENZIONE: Nessun controller rilevato in ascolto su 6633 o 6653. Uso porta di fallback: {actual_port}\n")

        # ----------------------------------------------------------------------
        # 2. INIZIALIZZAZIONE RETE MININET
        # ----------------------------------------------------------------------
        # switch=OVSKernelSwitch: esegue Open vSwitch nel modulo kernel di Linux per massime prestazioni
        # link=TCLink: abilita l'emulatore Linux Traffic Control (tc/netem) per applicare limiti di banda e delay
        info("*** Inizializzazione della rete Mininet\n")
        self.net = Mininet(controller=None, link=TCLink, switch=OVSKernelSwitch)

        # Configurazione dell'istanza RemoteController che punta al processo ryu-manager
        info(f"*** Configurazione del RemoteController (127.0.0.1:{actual_port})\n")
        self.c1 = self.net.addController(
            'c1',
            controller=RemoteController,
            ip='127.0.0.1',
            port=actual_port
        )
        self.c1.start()

        # ----------------------------------------------------------------------
        # 3. CREAZIONE DEGLI SWITCH OPENFLOW 1.3
        # ----------------------------------------------------------------------
        # dpid esplicito a 16 cifre esadecimali (es. '000...01' = 1) per garantire
        # perfetta corrispondenza con i nodi del grafo NetworkX nel controller Ryu.
        info("*** Creazione degli Switch OpenFlow 1.3\n")
        self.s1 = self.net.addSwitch('s1', dpid='0000000000000001', protocols='OpenFlow13')
        self.s2 = self.net.addSwitch('s2', dpid='0000000000000002', protocols='OpenFlow13')
        self.s3 = self.net.addSwitch('s3', dpid='0000000000000003', protocols='OpenFlow13')
        self.s4 = self.net.addSwitch('s4', dpid='0000000000000004', protocols='OpenFlow13')
        self.s5 = self.net.addSwitch('s5', dpid='0000000000000005', protocols='OpenFlow13')
        self.s6 = self.net.addSwitch('s6', dpid='0000000000000006', protocols='OpenFlow13')
 
        # ----------------------------------------------------------------------
        # 4. CREAZIONE DEGLI HOST CLIENT E SERVER BACKEND
        # ----------------------------------------------------------------------
        # Vengono assegnati IP e MAC statici deterministici per consentire
        # al controller Ryu di gestire l'ARP Proxy in modo affidabile senza ambiguità.
        info("*** Creazione dei Client e Server\n")
        self.h1 = self.net.addHost('h1', ip='10.0.0.1/24', mac='00:00:00:00:00:01')
        self.h2 = self.net.addHost('h2', ip='10.0.0.2/24', mac='00:00:00:00:00:02')
        self.h3 = self.net.addHost('h3', ip='10.0.0.3/24', mac='00:00:00:00:00:03')

        self.srv1 = self.net.addHost('srv1', ip='10.0.0.11/24', mac='00:00:00:00:00:11')
        self.srv2 = self.net.addHost('srv2', ip='10.0.0.12/24', mac='00:00:00:00:00:12')
        self.srv3 = self.net.addHost('srv3', ip='10.0.0.13/24', mac='00:00:00:00:00:13')

        # ----------------------------------------------------------------------
        # 5. COLLEGAMENTI ACCESS SWITCH S1 -> CLIENT
        # ----------------------------------------------------------------------
        # I parametri port1 e port2 forzano i numeri delle porte fisiche sugli switch:
        # S1: porta 1 -> h1, porta 2 -> h2, porta 3 -> h3
        info("*** Creazione Collegamenti Accesso Client -> S1\n")
        self.net.addLink(self.h1, self.s1, port1=1, port2=1)
        self.net.addLink(self.h2, self.s1, port1=1, port2=2)
        self.net.addLink(self.h3, self.s1, port1=1, port2=3)

        # ----------------------------------------------------------------------
        # 6. COLLEGAMENTI CORE PATH A (Capacità: 20 Mbps, Delay: 5 ms)
        # ----------------------------------------------------------------------
        # TCLink configura code HTB (Hierarchical Token Bucket) per limitare la banda a 20 Mbps
        # e modulo netem del kernel Linux per iniettare un ritardo artificiale di 5 ms su ciascun hop.
        info("*** Creazione Collegamenti Core Path A (BW: 20Mbps, Delay: 5ms)\n")
        self.pathA_1 = self.net.addLink(self.s1, self.s2, port1=4, port2=1, bw=20, delay='5ms')
        self.pathA_2 = self.net.addLink(self.s2, self.s4, port1=2, port2=1, bw=20, delay='5ms')
        self.pathA_3 = self.net.addLink(self.s4, self.s6, port1=2, port2=1, bw=20, delay='5ms')

        # ----------------------------------------------------------------------
        # 7. COLLEGAMENTI CORE PATH B (Capacità: 10 Mbps, Delay: 1 ms)
        # ----------------------------------------------------------------------
        # Canale alternativo: dimezza la capacità (10 Mbps) ma garantisce bassissima latenza (1 ms).
        info("*** Creazione Collegamenti Core Path B (BW: 10Mbps, Delay: 1ms)\n")
        self.pathB_1 = self.net.addLink(self.s1, self.s3, port1=5, port2=1, bw=10, delay='1ms')
        self.pathB_2 = self.net.addLink(self.s3, self.s5, port1=2, port2=1, bw=10, delay='1ms')
        self.pathB_3 = self.net.addLink(self.s5, self.s6, port1=2, port2=2, bw=10, delay='1ms')

        # ----------------------------------------------------------------------
        # 8. COLLEGAMENTI EGRESS SWITCH S6 -> SERVER BACKEND
        # ----------------------------------------------------------------------
        # S6: porta 1 -> S4 (Path A), porta 2 -> S5 (Path B)
        # S6: porta 3 -> srv1, porta 4 -> srv2, porta 5 -> srv3
        info("*** Creazione Collegamenti S6 -> Server Backend\n")
        self.net.addLink(self.srv1, self.s6, port1=1, port2=3)
        self.net.addLink(self.srv2, self.s6, port1=1, port2=4)
        self.net.addLink(self.srv3, self.s6, port1=1, port2=5)

        # ----------------------------------------------------------------------
        # 9. COSTRUZIONE E AVVIO DELLA RETE EMULATA
        # ----------------------------------------------------------------------
        info("*** Avvio effettivo della rete\n")
        self.net.build()  # Assegna indirizzi, crea le veth pair di Linux e istanzia i namespace
        self.net.start()  # Connette gli switch OpenFlow al controller Ryu specificato
    
    def stop(self):
        """Arresta in modo pulito l'emulazione liberando socket e interfacce virtuali del kernel."""
        info("*** Arresto della rete Mininet\n")
        self.net.stop()  


if __name__ == '__main__':
    # Imposta il livello di verbosità dei log di Mininet a 'info' per visualizzare tutti gli step
    setLogLevel('info')
    info('*** Avvio Environment di Test\n')
    env = Environment()
 
    try:
        info('*** Avvio Mininet CLI\n')
        CLI(env.net)  # Passa il controllo all'interfaccia a riga di comando (CLI) mininet>
    finally:
        # Il blocco finally garantisce che, anche in caso di eccezioni o Ctrl+C, la rete venga arrestata
        env.stop()
