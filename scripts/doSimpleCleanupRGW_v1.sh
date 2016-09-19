#!/bin/bash

if [[ $UID != 0 ]]; then
    echo "Please run this script with sudo:"
    echo "$0 $*"
    exit 1
fi

###############################################################################
# Remove supporting infrastructure for single instance of Realm Gateway
###############################################################################

# [COMMON]
## WAN side
ip link set dev br-wan0 down
ip link del dev br-wan0
ip link set dev br-wan1 down
ip link del dev br-wan1
# [RealmGateway-A]
## LAN side
ip link set dev br-lan0a down
ip link del dev br-lan0a


###############################################################################
# Create network namespace configuration
###############################################################################

#Create the default namespace
ln -s /proc/1/ns/net /var/run/netns/default > /dev/null 2> /dev/null

for i in hosta gwa router public; do
    #Remove namespaces
    ip netns del $i > /dev/null 2> /dev/null
done
