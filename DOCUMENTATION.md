# Introduction to CES (r)evolution 

This documents introduces the new framework for Customer Edge Switching (CES).
The system architecture has been redefined to improve performance.
The networking model is now fully based on iptables and OpenvSwitch for tunneling.

## Important Notes

iptables-extension list: http://manpages.ubuntu.com/manpages/trusty/man8/iptables-extensions.8.html
extensions to be considered:

* connlimit
* connmark
* cpu (For TCP Splice?)
* hashlimit
* limit
* mark
* recent
* physdev
* set (matching on ipset)
* tcpmss
* time
* statistics (nth match)
* string
* AUDIT
* CHECKSUM
* CONNMARK
* CT
* HMARK
* MIRROR - Removed from kernel
* NFQUEUE
* NFLOG?
* NOTRACK
* TCPMSS
* TEE
* TPROXY for TCPSplice?


## Networking Architecture

The following virtual network architecture has been devised to support the development
and testing of the CES platform.

### Virtual Networking

The model uses 3 different network interfaces with routing (Layer-3) capabilities.
* LAN: Connects to the private network where the local hosts are connected.
       e.g. 192.168.0.1/24
       e.g. 192.168.1.1/24
* WAN: Connects to the public network - Internet.
       e.g. 198.18.0.11/24 gateway: 198.18.0.1 - Circular Pool 198.18.0.[12-14]
       e.g. 198.18.0.21/24 gateway: 198.18.0.1 - Circular Pool 198.18.0.[22-24]
* TUN: Connects to the tunnel terminator (OpenvSwitch) for CES to CES communications.
       e.g. 1.1.1.1/32 NO ARP. Proxy Network 172.16.0.0/24 via tun-interface
       e.g. 1.1.1.2/32 NO ARP. Proxy Network 172.16.1.0/24 via tun-interface

### Network Interface Layout

+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
+                NETWORK DIAGRAM FOR CUSTOMER EDGE SWITCHING V0.2                   +
+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
+        LAN Network        |         TUN Network       |        WAN Network        +
+-----------------------------------------------------------------------------------+
+  l3-lana                  |  l3-tuna                  |  l3-wana                  |
+      |            (veth)  |      |            (veth)  |      |            (veth)  |
+  qve-l3-lana              |  qve-l3-tuna              |  qve-l3-wana              |
+     (+)                   |     (+)                   |     (+)                   |
+  qbf-lana          (br)   |  qbf-tuna          (br)   |  qbf-wana          (br)   |
+     (+)                   |     (+)                   |     (+)                   |
+  qve-phy-lana             |  qve-phy-tuna             |  qve-phy-wana             |
+      |            (veth)  |      |            (veth)  |      |            (veth)  |
+  qvi-phy-lana             |  qvi-phy-tuna             |  qvi-phy-wana             |
+     (+)                   |     (+)                   |     (+)                   |
+  qbi-lana          (br)   |  qbi-tuna          (ovs)  |  qbi-wan           (br)   |
+-----------------------------------------------------------------------------------+

Minimal requirements:
* l3-xxxx: Layer-3 routing interfaces with IP addressing. Virtual Ethernet pair.
* qve-l3-xxxx: Layer-2 Virtual Ethernet pair connected to Linux bridge.
* qbf-xxxx: Layer-2 Linux bridge connecting qve-l3 with qve-phy. Used for packet filtering in iptables.
* qve-phy-xxxx: Layer-2 Virtual Ethernet pair. Represents the "physical" network card.

Extended requirements for virtualization in same host:
* qve-phy-xxxx: Layer-2 Virtual Ethernet pair connected to filtering Linux bridge.
* qvi-phy-xxxx: Layer-2 Virtual Ethernet pair connected to integration Linux bridge.
* qbi-xxxx: Layer-2 Linux bridge connecting qvi-phy with qve-vhosts and network namespaces.


### Traffic directionality

Attending to the aforementioned network design we can distinguish between different routed traffic flows:

* LAN originated
  * To local CES services (DHCP and DNS)
  * To a host in the Internet - via l3-wan
  * To a host behind CES - via l3-tun (We could have different proxy pools for better control)
* WAN originated
  * To local CES services (HTTP and DNS)
  * To a host behind CES - via l3-lan
* TUN originated
  * To a host behind CES - via l3-lan

For the definition of rules and policies this is very important as it will allow us to define very specific scopes.

## Iptables 

### Iptables zones

There are a number of tweaks required in iptables for virtualizing several CES in the same machine without spawning Linux containers.
It would be possible to run the virtual CES in a Linux namespace, but certain iptables extensions do not appear to work properly, e.g. This 
is the case for TRACE(ing) packets and the LOG target.

To minimize the amount of tracked connections (conntrack), we will stop the tracking of packets in specific network interfaces.

Because the network architecture uses Linux bridges for packet filtering, we need to create different connection tracks when the packet flows through different interfaces and Linux bridges.
Therefore, we aggregate all Layer-3 routing interfaces in the same connection tracking zone (like a normal router would act) and create new tracking zones for each one of the other filtering scopes.
These scopes include WAN, LAN and TUN.

Within a CES we make use of 4 different connection tracking zones "CT zone".:
* Zone 1: The packet enters any Layer-3 routed interface l3-xxxx i.e. l3-wan, l3-lan, l3-tun.
* Zone 2: The packet enters the Linux filtering bridge qbf-wan for WAN qve-phy-wan or qve-l3-wan.
* Zone 3: The packet enters the Linux filtering bridge qbf-lan for LAN qve-phy-lan or qve-l3-lan.
* Zone 4: The packet enters the Linux filtering bridge qbf-tun for TUN qve-phy-tun or qve-l3-tun.

### Iptables chains

Depending on the input interface and tracking zone we will apply different rules.

Major packet filtering occurs when a packet enters the qbf-xxxx Linux bridge from a qve-phy-xxxx interface. This represents an incoming packet towards the CES.
The packet then traverses at least all iptables standard tables and chains, hitting a number of rules that may ACCEPT or DROP the packet, among other options.
If the incoming packet is ACCEPTed in the qbf-xxxx Linux bridge, it will continue it's way towards the Layer-3 routed interface l3-xxxx, where it will be processed
again by the kernel, this time entering a different connection track zone.


### Packet marking

Depending on the input interface we can set a mark on the packet to provide scope throughout the rest of the iptables processing. This mark can bet set to match with that of the connection tracking zone.
The packet marking takes place in the table raw and prerouting chain. The mark is a 32-bit integer that can be masked.
For example, we can use the 4-msbit of the 1-msbyte as follows:

* Zone 1: The packet is marked with 1-msbyte as 0x10/0xF0 -> 0x10000000/0xF0000000
* Zone 2: The packet is marked with 1-msbyte as 0x20/0xF0 -> 0x20000000/0xF0000000
* Zone 3: The packet is marked with 1-msbyte as 0x30/0xF0 -> 0x30000000/0xF0000000
* Zone 4: The packet is marked with 1-msbyte as 0x40/0xF0 -> 0x40000000/0xF0000000

Additionally, in the case of Linux filtering bridges, we can use the 4-lsbit of the 1-msbyte to indicate the direction of the packet when entering the bridge interface.
The direction is based on whether the packet is going-to (mark 0) or coming-from the associated Layer-3 routing interface.
* Zone 1: The packet is not handled by a Linux filtering bridges
* Zone 2: The packet enters the Linux filtering bridge qbf-wan
  * qve-phy-wan: The packet is marked with 1-msbyte as 0x00/0x0F -> 0x00000000/0x0F000000
  * qve-l3-wan:  The packet is marked with 1-msbyte as 0x01/0x0F -> 0x01000000/0x0F000000
* Zone 3: The packet enters the Linux filtering bridge qbf-lan
  * qve-phy-lan: The packet is marked with 1-msbyte as 0x00/0x0F -> 0x00000000/0x0F000000
  * qve-l3-tun:  The packet is marked with 1-msbyte as 0x01/0x0F -> 0x01000000/0x0F000000
* Zone 4: The packet enters the Linux filtering bridge qbf-tun
  * qve-phy-tun: The packet is marked with 1-msbyte as 0x00/0x0F -> 0x00000000/0x0F000000
  * qve-l3-tun:  The packet is marked with 1-msbyte as 0x01/0x0F -> 0x01000000/0x0F000000


## Protecting Customer Edge Switching

### DNS

### Protecting DNS

The following chains apply in qbf-xxxx Linux bridge when an incoming packet is UDP port 53:

* WanBlacklist: Banned sources and/or domains.
  * Match IP source and DROP
  * Match (S)FQDN and DROP - Use string extension (include !string SOA record)
* WanWhitelist: Trusted sources with SLA, e.g. Elisa DNS. Accept traffic per SLA, report excess and leak to Greylist_WK.
  * [Match IP source] LIMIT and ACCEPT
  * [Match IP source] LIMIT and LOG - INFO
  * [Match IP source] LIMIT and JUMP to Greylist_WK
* WanGreylist_WK: Known sources with high traffic and no-SLA plus leaky bucket from Whitelist, e.g. Google DNS, Elisa DNS.
  * [Match IP source] LIMIT and RETURN
  * [Match IP source] LIMIT and LOG - INFO
  * [Match IP source] LIMIT and JUMP to Greylist
* WanGreylist: Unknown sources plus leaky bucket from Greylist_WK, e.g. Random DNS, Google DNS.
  * [Match IP source] LIMIT and RETURN
  * [Match IP source] LIMIT and LOG - INFO
  * [Match IP source] LIMIT and DROP
* WanGlobalLimit: Apply global rate limits for Greylist_WK and Greylist "accepted" traffic.
  * T1 LIMIT and ACCEPT (Max rate from non whitelist)
  * 1/sec LIMIT and LOG - WARN
  * T2 LIMIT and DROP
  * 1/sec LIMIT and LOG - WARN
  * T3 LIMIT and REJECT
  * 1/sec LIMIT and LOG - ERRO
  * T4 LIMIT and DROP
* WanDomainLimit: Apply specific domain rate limit. The chain can be called to apply limits for Whitelist, Greylist_WK and Greylist ipsets.
  * Match (S)FQDN and LIMIT and RETURN - Use string extension (include !string SOA record)
  * Match (S)FQDN and LIMIT and LOG - INFO
  * Match (S)FQDN and LIMIT and DROP - INFO
  
* LanBlacklist: Banned sources and/or domains.
  * Match IP source and DROP
  * Match (S)FQDN and DROP - Use string extension
* LanCESLimit: Apply global admision rate limit in LAN network.
  * Match IP source and LIMIT-75% and ACCEPT
  * Match IP source and LIMIT-1/s and LOG - INFO
  * Match IP source and 1/4th DROP
  * Match IP source and 1/4th REJECT
  * Match IP source and DROP and LOG - WARN
  
Processing pipeline for incoming DNS packets from WAN at the Filtering Bridge:

* Jump to WanBlacklist chain.
* Jump to WanDomainLimit chain. (Maybe this should be called from the Greylist chains only?)
* Match WanWhitelist_ipset and jump to WanWhitelist chain.
* Match WanGreylistWK_ipset and jump to WanGreylist_WK chain.
* !Match WanGreylistWK_ipset and jump to WanGreylist chain.
* Jump to WanGlobalLimit chain. (Packet can be ACCEPT, DROP or REJECT)

Processing pipeline for incoming DNS packets from LAN at the Filtering Bridge:
* Jump to LanBlacklist chain.
* Jump to LanCESLimit chain.


### Protecting HTTP Proxy



### Protecting from TCP SYN Spoofed Attacks

### Protecting from (D)DoS SYN Spoofed Attacks



# Misc notes

#NF_DROP vs NF_STOLEN
http://stackoverflow.com/questions/19342950/what-is-the-difference-between-nf-drop-and-nf-stolen-in-netfilter-hooks

sudo iptables -F OUTPUT

# Matching on packet content with STRING module
## FQDN in DNS are encoded following a specific pattern.
## The first byte (06) is the length of google, followed by the 6 ASCII characters, then a count byte (03) for the length of com followed by... 
## http://blog.nintechnet.com/how-to-block-w00tw00t-at-isc-sans-dfind-and-other-web-vulnerability-scanners/

sudo iptables -A OUTPUT -m string --algo bm --hex-string "|06|google|03|com" -j DROP

# Parental control matching on time of day with TIME module
## Note time goes in UTC time, so beware of the current time zone!
## http://www.cyberciti.biz/tips/iptables-for-restricting-access-by-time-of-day.html

sudo iptables -A OUTPUT -d 8.8.8.8/32 -m time --timestart 12:00:00 --timestop 12:59:59 -j DROP


# Using ebtables for ARP Response?
http://ebtables.netfilter.org/examples/basic.html#ex_arpreply
ebtables -t nat -A PREROUTING -p arp --arp-opcode Request -j arpreply \
--arpreply-mac 10:11:12:13:14:15 --arpreply-target ACCEPT

# If we want to let the packet go all the way
ebtables -t nat -A PREROUTING -p arp --arp-opcode 1 --arp-ip-dst 198.18.0.27 -j arpreply --arpreply-mac 10:11:12:13:14:15 --arpreply-target ACCEPT
# If we want to stop the packet at the given interface
ebtables -t nat -A PREROUTING -p arp --arp-opcode 1 --arp-ip-dst 198.18.0.28 -j arpreply --arpreply-mac 10:11:12:13:14:15 --arpreply-target DROP
