#!/usr/bin/env python3

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


def create_topology():
    net = Mininet(
        controller=RemoteController,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True
    )

    info("*** Creazione controller remoto Ryu\n")
    c0 = net.addController(
        "c0",
        controller=RemoteController,
        ip="127.0.0.1",
        port=6633
    )

    info("*** Creazione host\n")
    h1 = net.addHost("h1", ip="10.0.0.1/24")  # client video
    h2 = net.addHost("h2", ip="10.0.0.2/24")  # client web
    h3 = net.addHost("h3", ip="10.0.0.3/24")  # attacker
    h4 = net.addHost("h4", ip="10.0.0.4/24")  # video server
    h5 = net.addHost("h5", ip="10.0.0.5/24")  # web server

    info("*** Creazione switch Open vSwitch\n")
    s1 = net.addSwitch("s1")
    s2 = net.addSwitch("s2")
    s3 = net.addSwitch("s3")
    s4 = net.addSwitch("s4")


    info("*** Collegamento host-switch\n")
    net.addLink(h1, s1, bw=10)
    net.addLink(h2, s1, bw=10)
    net.addLink(h3, s1, bw=10)

    net.addLink(h4, s4, bw=10)
    net.addLink(h5, s4, bw=10)

    info("*** Collegamento switch-switch\n")
    # Percorso video: banda maggiore
    net.addLink(s1, s2, bw=10)
    net.addLink(s2, s4, bw=10)

    # Percorso web/normale: banda minore
    net.addLink(s1, s3, bw=3)
    net.addLink(s3, s4, bw=3)

    info("*** Avvio rete\n")
    net.build()
    c0.start()

    for switch in [s1, s2, s3, s4]:
        switch.start([c0])

    info("*** Rete avviata\n")
    info("*** Host disponibili:\n")
    info("h1 = client video\n")
    info("h2 = client web\n")
    info("h3 = attacker\n")
    info("h4 = video server\n")
    info("h5 = web server\n")

    CLI(net)

    info("*** Arresto rete\n")
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    create_topology()
