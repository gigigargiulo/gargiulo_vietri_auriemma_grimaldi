from mininet.topo import Topo
from mininet.link import TCLink
import json

class MyTopo(Topo):
    def build(self):
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')

        h1 = self.addHost('h1', ip='10.0.0.1')
        h2 = self.addHost('h2', ip='10.0.0.2')
        h3 = self.addHost('h3', ip='10.0.0.3')
        h4 = self.addHost('h4', ip='10.0.0.4')
        h5 = self.addHost('h5', ip='10.0.0.5')
        h6 = self.addHost('h6', ip='10.0.0.6')
        h7 = self.addHost('h7', ip='10.0.0.7')
        h8 = self.addHost('h8', ip='10.0.0.8')

        # I link vengono aggiunti. L'ordine determina l'ID della porta sullo switch.
        self.addLink(h1, s1, cls=TCLink, bw=5)   # s1-eth1
        self.addLink(h2, s1, cls=TCLink, bw=5)   # s1-eth2
        self.addLink(h3, s2, cls=TCLink, bw=5)   # s2-eth1
        self.addLink(h4, s2, cls=TCLink, bw=5)   # s2-eth2
        self.addLink(s1, s3, cls=TCLink, bw=20)  # s1-eth3 <-> s3-eth1
        self.addLink(s2, s3, cls=TCLink, bw=20)  # s2-eth3 <-> s3-eth2
        self.addLink(h6, s3, cls=TCLink, bw=70)  # s3-eth3
        self.addLink(h7, s3, cls=TCLink, bw=70)  # s3-eth4
        self.addLink(h8, s3, cls=TCLink, bw=70)  # s3-eth5
        self.addLink(s3, h5, cls=TCLink, bw=100) # s3-eth6

        # Mappa corretta basata sulla topologia effettiva (switch, porta) -> bw
        port_bw = {
            "1,1": 5,    # s1 -> h1
            "1,2": 5,    # s1 -> h2
            "1,3": 20,   # s1 -> s3
            
            "2,1": 5,    # s2 -> h3
            "2,2": 5,    # s2 -> h4
            "2,3": 20,   # s2 -> s3
            
            "3,1": 20,   # s3 -> s1
            "3,2": 20,   # s3 -> s2
            "3,3": 70,   # s3 -> h6
            "3,4": 70,   # s3 -> h7
            "3,5": 70,   # s3 -> h8
            "3,6": 100   # s3 -> h5
        }


topos = { 'mytopo': ( lambda: MyTopo() ) }