from mininet.net import Mininet #serve a gestire l'intera topologia di rete
from mininet.node import RemoteController, OVSKernelSwitch #RemoteController controller non interno a mininet ma RYU quindi external
#OVSKernelSwitch: serve a instanziare switch virtuali su Open vSwitch
from mininet.link import TCLink # mi fa applicare i parametri di qualita'
from mininet.cli import CLI # serve per pingall iperf ecc quindi terminale mininet
from mininet.log import setLogLevel, info # serve per l'output del terminale

# PARAMETRI DEI LINK
# i link host-switch NON sono limitati in banda: il collo di bottiglia deve
# essere il core, altrimenti lo slicing non e' misurabile con iperf
HOST_DELAY = '0.1ms'

UPPER_BW = 10       # percorso S2
UPPER_DELAY = '2ms'

LOWER_BW = 1        # percorso S3
LOWER_DELAY = '5ms'
# i ritardi diversi sono un modo per identificare lo slice

QUEUE_SIZE = 100    # limita l'accumulo in coda (bufferbloat) sui link core


def build_network():
    net = Mininet(controller=RemoteController, switch=OVSKernelSwitch,
                  link=TCLink, autoSetMacs=False, build=False)
        # autoSetMacs=False MAC fissati da noi, questo serve al controller
        # build=False perche' la rete va costruita dopo aver dichiarato tutto

    # metto il remote controller RYU in localhost alla porta standard di OpenFlow
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)

    # creo gli host con i MAC statici (4 host) che servono a RYU:
    # il controller ragiona sui MAC, quindi non possono essere casuali
    h1 = net.addHost('h1', mac='00:00:00:00:00:01', ip='10.0.0.1/24')
    h2 = net.addHost('h2', mac='00:00:00:00:00:02', ip='10.0.0.2/24')
    h3 = net.addHost('h3', mac='00:00:00:00:00:03', ip='10.0.0.3/24')
    h4 = net.addHost('h4', mac='00:00:00:00:00:04', ip='10.0.0.4/24')

    # aggiungo gli switch OpenFlow 1.3
    s1 = net.addSwitch('s1', protocols='OpenFlow13')   # edge lato h1/h2
    s2 = net.addSwitch('s2', protocols='OpenFlow13')   # core percorso UPPER
    s3 = net.addSwitch('s3', protocols='OpenFlow13')   # core percorso LOWER
    s4 = net.addSwitch('s4', protocols='OpenFlow13')   # edge lato h3/h4

    # creazione link host-switch
        # i numeri di porta sono FISSATI esplicitamente e il controller li usa
        # come costanti: cosi' non dipendo dal fatto che Mininet numeri le
        # porte in ordine di creazione
    net.addLink(h1, s1, port1=0, port2=1, delay=HOST_DELAY)
    net.addLink(h2, s1, port1=0, port2=2, delay=HOST_DELAY)
    net.addLink(h3, s4, port1=0, port2=1, delay=HOST_DELAY)
    net.addLink(h4, s4, port1=0, port2=2, delay=HOST_DELAY)

    # link core UPPER (10 Mbps, via s2)
    net.addLink(s1, s2, port1=3, port2=1, bw=UPPER_BW, delay=UPPER_DELAY,
                max_queue_size=QUEUE_SIZE)
    net.addLink(s2, s4, port1=2, port2=3, bw=UPPER_BW, delay=UPPER_DELAY,
                max_queue_size=QUEUE_SIZE)

    # link core LOWER (1 Mbps, via s3)
    net.addLink(s1, s3, port1=4, port2=1, bw=LOWER_BW, delay=LOWER_DELAY,
                max_queue_size=QUEUE_SIZE)
    net.addLink(s3, s4, port1=2, port2=4, bw=LOWER_BW, delay=LOWER_DELAY,
                max_queue_size=QUEUE_SIZE)

    # costruzione e avvio della rete
    net.build()
    net.start()

    # disabilito IPv6: Linux genera traffico multicast automatico (NDP, MLD)
    # con MAC sconosciuti al controller, che sporcano i log
    info('*** Disattivo IPv6 su host e switch\n')

    # primo for per tutti gli host della rete
    for host in net.hosts:
        host.cmd('sysctl -w net.ipv6.conf.all.disable_ipv6=1')     # interfacce presenti
        host.cmd('sysctl -w net.ipv6.conf.default.disable_ipv6=1') # e future

    # secondo for per gli switch della rete
    for sw in net.switches:
        # itero sulle interfacce dello switch
        for intf in sw.intfList():
            # escludo la loopback (lo), su cui IPv6 non da' fastidio
            if intf.name != 'lo':
                sw.cmd('sysctl -w net.ipv6.conf.%s.disable_ipv6=1' % intf.name)

    return net

# main che parte solo se lancio il file da prompt
if __name__ == '__main__':
    setLogLevel('info')       # setto i log
    net = build_network()
    CLI(net)                  # apro la riga di comando di Mininet
    net.stop()                # uscita pulita: arresta switch/host/link e
                              # ripristina le interfacce di rete
