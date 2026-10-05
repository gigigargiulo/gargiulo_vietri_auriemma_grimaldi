
from mininet.topo import Topo

class QoSTopo(Topo):
	#metodo per costruire la rete
	def build(self):
		s1 = self.addSwitch('s1')

		h1 = self.addHost('h1') #sorgente UDP
		h2 = self.addHost('h2') #sorgente TCP
		h3 = self.addHost('h3') # destinazione comune

		#aggiungo i link
		self.addLink(h1,s1, bw = 5)
		self.addLink(h2,s1, bw = 5) 
		self.addLink(h3,s1, bw = 5)

topos = { 'qostopo': (lambda:QoSTopo()) }

