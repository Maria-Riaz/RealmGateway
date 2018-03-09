import asyncio
import logging
import time
import functools
import ipaddress

from helpers_n_wrappers import container3
from helpers_n_wrappers import utils3

import host
from host import KEY_SERVICE_SFQDN

import connection
from connection import ConnectionLegacy

import dns
import dns.message
import dns.rcode

from dns.rdataclass import *
from dns.rdatatype import *

# Reputation and system load
PBRA_REPUTATION_MIDDLE = 0.45
SYSTEM_LOAD_ENABLED   = lambda x: x>=0

# Keys for uStateDNSResolver
KEY_DNSNODE_IPADDR  = 10
KEY_DNSHOST_NCID    = 11

# Keys for uStateDNSGroup
KEY_DNSGROUP        = 20
KEY_DNSGROUP_ID     = 21
KEY_DNSGROUP_IPADDR = 22

# Keys for uDNSQueryTimer
KEY_TIMER           = 30
KEY_TIMER_FQDN      = 31

# Keys for uStateDNSHost
KEY_DNSHOST_IPADDR  = 40

# Common keys for reputation objects
KEY_DNS_REPUTATION  = 50

# Keys for uStateDataPacket
KEY_DATA_PACKET     = 60

# TODO: Create minimal unit that extends ContainerNode and implements a uReputation object with basic methods to avoid code duplication

class uReputation(object):
    """
    # Define an initial reputation value when we do not have any data
    initial_reputation = 0.45

    # These values define penalty/reward factor for higher loads of traffic
    # The factor is used as the exponent using the total number of events as the base
    ok_factor = 0
    ok_factor = 0.15
    neutral_factor = 0
    """

    def __init__(self, initial_reputation=0.45, ok_factor=0, nok_factor=0.15, neutral_factor=0):
        self.name = id(self)
        self.initial_reputation = initial_reputation
        self.ok_factor = ok_factor
        self.nok_factor = nok_factor
        self.neutral_factor = neutral_factor
        # Define counters
        self.ok = 0
        self.nok = 0
        self.neutral = 0
        self.total = 0
        self.trusted = 0
        self.untrusted = 0
        # Create dummy events upon initialization
        [self.event_neutral() for _ in range(5)]

    def event_ok(self):
        self.ok += 1
        self.total += 1

    def event_nok(self):
        self.nok += 1
        self.total += 1

    def event_neutral(self):
        self.neutral += 1
        self.total += 1

    def event_trusted(self):
        self.trusted += 1

    def event_untrusted(self):
        self.untrusted += 1

    @property
    def _ok_factor(self):
        # Define a ok_factor that rewards ok event
        return self.total ** self.ok_factor

    @property
    def _nok_factor(self):
        # Define a nok_factor that penalizes nok events
        return self.total ** self.nok_factor

    @property
    def _neutral_factor(self):
        # Define a nok_factor that penalizes nok events
        return self.total ** self.neutral_factor

    @property
    def reputation(self):
        """ Calculate reputation value based only on locally recorded events """
        try:
            rep = 0.5 * self._ok_factor * (self.ok / self.total) - \
                  0.5 * self._nok_factor * (self.nok / self.total) + \
                  self.initial_reputation * self._neutral_factor
        except ZeroDivisionError as e:
            rep = self.initial_reputation

        # Normalize reputation values between [0,1]
        if rep <= 0:
            return 0
        elif rep >= 1:
            return 1
        else:
            return rep

    def merge(self, other):
        # Merge self reputation values with other
        [self.event_ok()        for i in range(other.ok)]
        [self.event_nok()       for i in range(other.nok)]
        [self.event_neutral()   for i in range(other.neutral)]
        [self.event_trusted()   for i in range(other.trusted)]
        [self.event_untrusted() for i in range(other.untrusted)]

    def __repr__(self):
        return '[{}] neutral={} ok={} nok={} reputation={:.3f} / trusted={} untrusted={}'.format(self.name, self.neutral, self.ok, self.nok, self.reputation, self.trusted, self.untrusted)


class uDNSQueryTimer(container3.ContainerNode):
    TIMEOUT = 8.0

    def __init__(self, query, ipaddr, service, alias_service, timeout=0):
        """ Initialize as a ContainerNode """
        # Initialize super
        super().__init__('uDNSQueryTimer')
        self.query = query
        self.ipaddr = ipaddr
        self.service = service
        self.alias_service = alias_service
        self.timeout = timeout
        # Set default timeout if not overriden
        if not self.timeout:
            self.timeout = uDNSQueryTimer.TIMEOUT
        # Take creation timestamp
        self.timestamp_zero = time.time()
        self.timestamp_eol = self.timestamp_zero + self.timeout
        # Create DNS cache dictionary (k,v) -> (rdtype, rdata)
        self.cache = {}
        # Use a flag
        self.active = False

    def hasexpired(self):
        """ Return True if the timeout has expired """
        return time.time() > self.timestamp_eol

    def lookupkeys(self):
        """ Return the lookup keys """
        return [((KEY_TIMER), False),
                ((KEY_TIMER_FQDN, self.alias_service['fqdn']), True)]

    def __repr__(self):
        return '[{}] resolver={} service={} alias_service={} timeout={} sec'.format(self._name, self.ipaddr, self.service, self.alias_service, self.timeout)


class uStateDNSHost(container3.ContainerNode):
    """ This class defines a DNS advertised node via EDNS0 ClientSubnet / Extended Client Information / Name Client Identifier """
    def __init__(self, **kwargs):
        super().__init__('uStateDNSHost')
        ## IP source / EDNS0 ClientSubnet / Extended Client Information
        self.ipaddr      = None
        self.ipaddr_mask = 32
        ## EDNS0 Name Client Identifier -> Tuple of (tag_id, dns_group_id)
        self.ncid        = (None, None)
        # Override attributes
        utils3.set_attributes(self, override=True, **kwargs)
        ## Convert IPaddr/mask to network address
        self._ipaddr = ipaddress.ip_network('{}/{}'.format(self.ipaddr, self.ipaddr_mask), strict=False)
        # Overwrite ipaddr with network address
        self.ipaddr = format(self._ipaddr.network_address)

        # Define reputation parameters
        self.initial_reputation = PBRA_REPUTATION_MIDDLE
        self.period_n = 0
        self.period_ts = time.time()
        self.weight_previous = 0.25
        self.weight_current = 0.75
        # Create reputation objects
        self.reputation_current = uReputation(initial_reputation = self.initial_reputation)
        self.reputation_previous = uReputation(initial_reputation = self.initial_reputation)

    def lookupkeys(self):
        """ Return the lookup keys """
        # Return an iterable (key, isunique)
        keys = []
        # Typical keys of an advertised DNS host and data host
        keys.append(((KEY_DNSHOST_IPADDR, self.ipaddr), True))
        keys.append(((KEY_DNSHOST_NCID, self.ncid), True))
        # Common key for indexing all reputation objects
        keys.append((KEY_DNS_REPUTATION, False))
        return keys

    def contains(self, ipaddr):
        """ Return True if ipaddr exists in the defined network """
        try:
            return ipaddress.ip_address(ipaddr) in self._ipaddr
        except:
            return False

    def __repr__(self):
        return '[{}] ipaddr={}/{} ncid={} / reputation previous={:.3f} current={:.3f} weighted_avg={:.3f}'.format(self._name, self.ipaddr, self.ipaddr_mask, self.ncid, self.reputation_previous.reputation, self.reputation_current.reputation, self.reputation)

    def transition_period(self):
        # Transition to next period
        self.period_n += 1
        self.period_ts = time.time()
        # Use current reputation for computing next period's
        _reputation = self.reputation
        # Transition current reputation object into previous
        del self.reputation_previous
        self.reputation_previous = self.reputation_current

        # Calculate absolute distance for ageing
        distance = abs(_reputation - PBRA_REPUTATION_MIDDLE)
        ## Age reputation towards the "middle point"
        if _reputation < PBRA_REPUTATION_MIDDLE:
            _reputation += distance / 3
        elif _reputation > PBRA_REPUTATION_MIDDLE:
            _reputation -= distance / 3

        # Create new reputation object for current period
        self.reputation_current = uReputation(initial_reputation = _reputation)

    @property
    def reputation(self):
        return self.weight_previous * self.reputation_previous.reputation + \
               self.weight_current * self.reputation_current.reputation

    def event_ok(self):
        self.reputation_current.event_ok()

    def event_nok(self):
        self.reputation_current.event_nok()

    def event_neutral(self):
        self.reputation_current.event_neutral()

    def event_trusted(self):
        self.reputation_current.event_trusted()

    def event_untrusted(self):
        self.reputation_current.event_untrusted()


class uStateDNSResolver(container3.ContainerNode):
    """ This class stores the state information available for any DNS resolver node """
    def __init__(self, **kwargs):
        super().__init__('uStateDNSResolver')
        # Set default attributes
        self.edns0_cookie = None
        self.supported_edns0 = []
        self.ipaddr = None
        # Override attributes
        utils3.set_attributes(self, override=True, **kwargs)
        # Sanity check
        assert(self.ipaddr is not None)

    def lookupkeys(self):
        """ Return the lookup keys """
        # Return an iterable (key, isunique)
        keys = []
        # Create unique key based on IP address literal
        keys.append(((KEY_DNSNODE_IPADDR, self.ipaddr), True))
        return keys

    def __repr__(self):
        return '[{}] ipaddr={}'.format(self._name, self.ipaddr)


class uStateDNSGroup(container3.ContainerNode):
    """ This class stores the state information available for any DNS node (resolver or requestor) """
    def __init__(self, **kwargs):
        super().__init__('uStateDNSGroup')

        # Set default attributes
        self.period_n = 0
        self.period_ts = time.time()

        # Define weighted values for reputation calculation based on historic data
        self.weight_previous = 0.25
        self.weight_current = 0.75

        # Define initial reputation
        self.initial_reputation = PBRA_REPUTATION_MIDDLE

        # Define flag to indicate SLA agreement for use of Extended Client Subnet / Extended Client Information
        self.sla = False

        # Define a list for uStateDNSResolver ipaddresses
        self.nodes = []

        # Override attributes
        utils3.set_attributes(self, override=True, **kwargs)

        # Create DNSGroup id
        self.group_id = id(self)

        # Create reputation objects
        self.reputation_current = uReputation(initial_reputation = self.initial_reputation)
        self.reputation_previous = uReputation(initial_reputation = self.initial_reputation)

    def lookupkeys(self):
        """ Return the lookup keys """
        # Return an iterable (key, isunique)
        keys = []
        # Create default key to index all same-element
        keys.append((KEY_DNSGROUP, False))
        # Create unique key based on ID of object
        keys.append(((KEY_DNSGROUP_ID, self.group_id), True))
        # Create unique key based on IP address literal
        for ipaddr in self.nodes:
            keys.append(((KEY_DNSGROUP_IPADDR, ipaddr), True))
        # Common key for indexing all reputation objects
        keys.append((KEY_DNS_REPUTATION, False))
        return keys

    def transition_period(self):
        # Transition to next period
        self.period_n += 1
        self.period_ts = time.time()
        # Use current reputation for computing next period's
        _reputation = self.reputation
        # Transition current reputation object into previous
        del self.reputation_previous
        self.reputation_previous = self.reputation_current

        # Calculate absolute distance for ageing
        distance = abs(_reputation - PBRA_REPUTATION_MIDDLE)
        ## Age reputation towards the "middle point"
        if _reputation < PBRA_REPUTATION_MIDDLE:
            _reputation += distance / 3
        elif _reputation > PBRA_REPUTATION_MIDDLE:
            _reputation -= distance / 3

        # Create new reputation object for current period
        self.reputation_current = uReputation(initial_reputation = _reputation)

    @property
    def reputation(self):
        return self.weight_previous * self.reputation_previous.reputation + \
               self.weight_current * self.reputation_current.reputation

    def event_ok(self):
        self.reputation_current.event_ok()

    def event_nok(self):
        self.reputation_current.event_nok()

    def event_neutral(self):
        self.reputation_current.event_neutral()

    def event_trusted(self):
        self.reputation_current.event_trusted()

    def event_untrusted(self):
        self.reputation_current.event_untrusted()

    def __repr__(self):
        return '[{}] period={} ipaddrs={} sla={} / reputation previous={:.3f} current={:.3f} weighted_avg={:.3f}'.format(self._name, self.period_n, self.nodes, self.sla,
                                                                                                          self.reputation_previous.reputation,
                                                                                                          self.reputation_current.reputation,
                                                                                                          self.reputation)

    def show_reputation(self):
        print(self)
        print('  >> previous: {}'.format(self.reputation_previous))
        print('  >> current:  {}'.format(self.reputation_current))

    def merge(self, other):
        self._logger.info('Merging 2 DNS groups: \n{}\n{}'.format(self, other))
        # Combine nodes
        for ipaddr in other.nodes:
            self.nodes.append(ipaddr)

        # Calculate last period value and transition reputations to catch up
        last_period_n = max(self.period_n, other.period_n)
        ## Age myself to catch up with last period
        for i in range(self.period_n, last_period_n):
            self.transition_period()
        ## Age other to catch up with last period
        for i in range(other.period_n, last_period_n):
            other.transition_period()

        # Update self reputation values with other
        ## Previous reputation period
        self.reputation_previous.merge(other.reputation_previous)
        ## Current reputation period
        self.reputation_current.merge(other.reputation_current)

        # Update SLA values
        if self.sla or other.sla:
            self.sla = True

class uStateDataPacket(container3.ContainerNode):
    """ This class stores the packet information available for any data source """
    def __init__(self, src, dst):
        # Receive an exploded dict of packet_fields
        super().__init__('uStateDataPacket')
        self.src = src
        self.dst = dst
        # Use dictionary to record seen packets
        self.state = {}

    def lookupkeys(self):
        """ Return the lookup keys """
        # Return an iterable (key, isunique)
        keys = []
        # Create unique key based on IP src and dst
        keys.append(((KEY_DATA_PACKET, (self.src, self.dst)), True))
        return keys

    def hasexpired(self):
        """ Return True if the timeout has expired """
        ts_now = time.time()
        delete_keys = []
        for key, (_n, _t) in self.state.items():
            if ts_now < _t:
                continue
            delete_keys.append(key)
        for key in delete_keys:
            del self.state[key]

    def _generate_packet_key(self, **kwargs):
        _proto = kwargs['proto']
        if _proto == 1:
            _type, _code = kwargs['icmp-type'], kwargs['icmp-code']
            key = (_type, _code)
        elif _proto == 6:
            _sport, _dport = kwargs['sport'], kwargs['dport']
            _seq, _ack, _flags = kwargs['tcp_seq'], kwargs['tcp_ack'], kwargs['tcp_flags']
            key = (_sport, _dport, _seq, _ack)
        elif _proto == 17:
            _sport, _dport = kwargs['sport'], kwargs['dport']
            key = (_sport, _dport)
        elif _proto == 132:
            _sport, _dport = kwargs['sport'], kwargs['dport']
            _tag = kwargs['sctp_tag']
            key = (_sport, _dport)
        else:
            return _proto
        return key

    def has_record(self, **kwargs):
        key = self._generate_packet_key(**kwargs)
        return (key in self.state)

    def add_record(self, **kwargs):
        key = self._generate_packet_key(**kwargs)
        # Use a TTL of 20 seconds per record
        ttl = 20
        ts_eol = time.time() + ttl
        if key in self.state:
            _n, _t = self.state[key]
            self.state[key] = (_n+1, ts_eol)
        else:
            self.state[key] = (1, ts_eol)

    def get_record(self, **kwargs):
        key = self._generate_packet_key(**kwargs)
        assert(key in self.state)
        return self.state[key]

    def __repr__(self):
        return '[{}] src={} dst={}\n{}'.format(self._name, self.src, self.dst, self.state)


class PolicyBasedResourceAllocation(container3.Container):
    """
    Control variable

    * PBRA_DNS_POLICY_TCPCNAME establishes that allocation are only allowed for CNAMEs via TCP
        If enabled, overrides previous policies: PBRA_DNS_POLICY_TCP and PBRA_DNS_POLICY_CNAME

    * PBRA_DNS_POLICY_TCP establishes that incoming first queries must be carried via TCP
    * PBRA_DNS_POLICY_CNAME establishes that allocation is only allowed via temporary alias names of CNAME responses
        These two policies can be enabled or disabled independently
    * PBRA_DNS_LOG_UNTRUSTED enables logging all untrsuted UDP DNS query attempts
    * PBRA_DNS_LOAD_POLICING enables dynamic fine-grained policy enforcement based on system load

    # Load levels in 100% (Use -1 value in threshold parameter to disable step)
    ## math choices 'min', 'max', 'avg'
    {'threshold': 80, 'fqdn_new': 1.0, 'sfqdn_new': 0.8, 'sfqdn_reuse': 0.7, 'math': 'min'}

    """

    # Define default policy control variables
    PBRA_DNS_POLICY_TCPCNAME  = False
    PBRA_DNS_POLICY_TCP       = False
    PBRA_DNS_POLICY_CNAME     = False
    PBRA_DNS_LOG_UNTRUSTED    = False
    PBRA_DNS_LOAD_POLICING    = False
    # Define default system load policies
    SYSTEM_LOAD = [{'threshold': 80, 'fqdn_new': 1.0, 'sfqdn_new': 0.8, 'sfqdn_reuse': 0.7, 'math': 'min'},
                   {'threshold': 66, 'fqdn_new': 0.6, 'sfqdn_new': 0.4, 'sfqdn_reuse': 0.3, 'math': 'avg'},
                   {'threshold': 33, 'fqdn_new': 0.3, 'sfqdn_new': 0.2, 'sfqdn_reuse': 0.1, 'math': 'max'},
                   {'threshold':  0, 'fqdn_new': 0.2, 'sfqdn_new': 0.1, 'sfqdn_reuse': 0.0, 'math': 'max'},
                   {'threshold': -1, 'fqdn_new': 0.0, 'sfqdn_new': 0.0, 'sfqdn_reuse': 0.0, 'math': 'min'} # This is disabled
                   ]


    def __init__(self, **kwargs):
        """ Initialize as a Container """
        super().__init__('PolicyBasedResourceAllocation')
        # Override attributes
        utils3.set_attributes(self, override=True, **kwargs)
        # Load CircularPool control variables
        self._init_circularpool_control_variables()
        # Load CircularPool pre configured DNS groups
        self._init_dns_group_policy()

    def _init_circularpool_control_variables(self):
        # Initialize System Load Policy threshold values
        cpool_policy = self.datarepository.get_policy_ces('CIRCULARPOOL', None)

        if cpool_policy is None or 'CONTROL_VARIABLES' not in cpool_policy:
            self._logger.warning('Using default CONTROL_VARIABLES values')
            return

        # Set all existing values of the policy
        kwargs = cpool_policy['CONTROL_VARIABLES']
        utils3.set_attributes(self, override=True, **kwargs)
        # Reverse sort SYSTEM_LOAD levels for sanity
        self.SYSTEM_LOAD = sorted(self.SYSTEM_LOAD, key=lambda entry: entry['threshold'], reverse=True)

        # Show running control variables
        self._logger.info('Control variable: {}={}'.format('PBRA_DNS_POLICY_TCPCNAME', self.PBRA_DNS_POLICY_TCPCNAME))
        self._logger.info('Control variable: {}={}'.format('PBRA_DNS_POLICY_TCP', self.PBRA_DNS_POLICY_TCP))
        self._logger.info('Control variable: {}={}'.format('PBRA_DNS_POLICY_CNAME', self.PBRA_DNS_POLICY_CNAME))
        self._logger.info('Control variable: {}={}'.format('PBRA_DNS_LOG_UNTRUSTED', self.PBRA_DNS_LOG_UNTRUSTED))
        self._logger.info('Control variable: {}={}'.format('PBRA_DNS_LOAD_POLICING', self.PBRA_DNS_LOAD_POLICING))
        self._logger.info('Control variable: {}=\n{}'.format('SYSTEM_LOAD', '\n'.join(format(_) for _ in self.SYSTEM_LOAD)))


    def _init_dns_group_policy(self):
        # Initialize DNS Group Policy with bootstrapping values
        cpool_policy = self.datarepository.get_policy_ces('CIRCULARPOOL', None)

        if cpool_policy is None or 'DNS_GROUP_POLICY' not in cpool_policy:
            self._logger.warning('Bootstrapping CIRCULARPOOL.DNS_GROUP_POLICY not found')
            return

        for dnsgroup_kwargs in cpool_policy['DNS_GROUP_POLICY']:
            # Create new DNS group with single DNS node
            self._logger.debug('Create new DNS group: {}'.format(dnsgroup_kwargs))
            dnsgroup_obj = uStateDNSGroup(**dnsgroup_kwargs)
            self.add(dnsgroup_obj)

            # Iterate node IP addresses and create new DNS nodes
            for ipaddr in dnsgroup_obj.nodes:
                dnsnode_obj = uStateDNSResolver(ipaddr=ipaddr)
                self.add(dnsnode_obj)

            # Update keys after nodes changes
            self.updatekeys(dnsgroup_obj)

    def cleanup_timers(self):
        """ Perform a cleanup of expired timer objects """
        nodes = self.lookup(KEY_TIMER, update=False, check_expire=False)
        if nodes is None:
            return
        for node in list(nodes):
            if node.hasexpired():
                self.remove(node)

    def debug_dnsgroups(self, transition = False):
        # For debugging purposes
        #nodes = self.lookup(KEY_DNSGROUP, update=False, check_expire=False)
        nodes = self.lookup(KEY_DNS_REPUTATION, update=False, check_expire=False)
        if nodes is None:
            return

        if transition:
            [node.transition_period() for node in nodes]
            [print(node) for node in nodes]
            #[node.show_reputation() for node in nodes]


    def _policy_tcp(self, query):
        # Answer TRUNCATED
        response = dns.message.make_response(query, recursion_available=False)
        response.set_rcode(dns.rcode.NOERROR)
        response.flags |= dns.flags.TC
        return response

    def _policy_cname(self, query):
        # Answer CNAME
        fqdn = format(query.question[0].name)
        """
        There seems to be a bug using LXC and string match.
        The longest match that works is 73 chars, i.e. z1234567890123456789012345678901234567890123.test.gwa.cesproto.re2ee.org.
        The shortest match that does not work is 74 chars, i.e. z12345678901234567890123456789012345678901234.test.gwa.cesproto.re2ee.org.
        The ideal MAX_LENGTH_LABEL should have value of 63. For compatibility, we use 32.
        """
        MAX_LENGTH_LABEL = 32
        _fqdn = utils3.random_string(MAX_LENGTH_LABEL) + '.' + fqdn
        ttl = 0
        response = dns.message.make_response(query, recursion_available=False)
        response.set_rcode(dns.rcode.NOERROR)
        response.answer = [dns.rrset.from_text(fqdn, ttl, 1, dns.rdatatype.CNAME, _fqdn)]
        return response, _fqdn

    def _load_metadata_resolver(self, query, addr, create=False):
        # Collect metadata from DNS query related to resolver based on IP address
        if self.has((KEY_DNSGROUP_IPADDR, addr[0])) is False and create is True:
            # Create resolver reputation of the DNS query
            ## Create new DNS node
            dnsnode_obj = uStateDNSResolver(ipaddr=addr[0])
            self.add(dnsnode_obj)
            ## Create new DNS group with single DNS node
            dnsgroup_obj = uStateDNSGroup()
            dnsgroup_obj.nodes.append(dnsnode_obj.ipaddr)
            self.add(dnsgroup_obj)
            # Add reputation to the DNS query
            query.reputation_resolver = dnsgroup_obj
            return
        else:
            # Add reputation to the DNS query
            dnsgroup_obj = self.lookup((KEY_DNSGROUP_IPADDR, addr[0]))
            query.reputation_resolver = dnsgroup_obj

    def _load_metadata_requestor(self, query, addr, create=False):
        # TODO: Bind ncid based on specific dns group id instead of resolver ipaddr (as to respect EDNS0 NCID cluster specification)

        # Collect metadata from DNS query related to requestor based on DNS options (EDNS0)
        dnshost_obj = None
        meta_ipaddr = None
        meta_ncid = None
        meta_flag = False

        for opt in query.options:
            if hasattr(opt, 'to_text'):
                self._logger.debug('Found EDNS0: {}'.format(opt.to_text()))
            else:
                self._logger.debug('Found EDNS0: Generic {}'.format(opt.otype))

            if opt.otype == 0x08 and meta_ipaddr is None:
                # ClientSubnet
                meta_ipaddr = opt.address
                meta_mask   = opt.srclen
                meta_flag   = True
            elif opt.otype == 0xff01:
                # ExtendedClientInformation (preferred)
                meta_ipaddr = opt.address
                meta_mask   = 32
                meta_flag   = True
            elif opt.otype == 0xff02:
                # ExtendedClientInformation
                meta_ncid   = opt.id_data
                meta_flag   = True

        if meta_flag is False:
            query.reputation_requestor = None
            return

        ipaddr_lookupkey = format(ipaddress.ip_network('{}/{}'.format(meta_ipaddr, meta_mask), strict=False).network_address)
        ncid_lookupkey = (meta_ncid, addr[0]) # tuple of ncid tag and resolver IP address

        if self.has((KEY_DNSHOST_IPADDR, ipaddr_lookupkey)):
            # Get existing object
            dnshost_obj = self.get((KEY_DNSHOST_IPADDR, ipaddr_lookupkey))
            self._logger.debug('Retrieved existing uStateDNSHost for requestor ipaddr={}/{}'.format(meta_ipaddr, meta_mask))

        elif self.has((KEY_DNSHOST_IPADDR, ipaddr_lookupkey)) is False and create is True:
            self._logger.info('Create uStateDNSHost for requestor ipaddr={}/{}'.format(meta_ipaddr, meta_mask))
            dnshost_obj = uStateDNSHost(ipaddr = meta_ipaddr, ipaddr_mask = meta_mask)
            self.add(dnshost_obj)

        # Needs to be tested
        elif self.has((KEY_DNSHOST_NCID, ncid_lookupkey)):
            # Get existing object
            dnshost_obj = self.get((KEY_DNSHOST_NCID, ncid_lookupkey))
            self._logger.debug('Retrieved existing uStateDNSHost for requestor ncid={}@{}'.format(meta_ncid, addr[0]))

        elif self.has((KEY_DNSHOST_NCID, ncid_lookupkey)) is False and create is True:
            self._logger.info('Create uStateDNSHost for requestor ncid={}@{}'.format(meta_ncid, addr[0]))
            dnshost_obj = uStateDNSHost(ncid = ncid_lookupkey)
            self.add(dnshost_obj)

        # Add reputation to the DNS query
        query.reputation_requestor = dnshost_obj

    def _dns_preprocess_rgw_wan_soa_event_logging(self, query, alias = False):
        """ Perform event logging based on trustworthiness of DNS query """
        # Log only when pre-conditions are met
        if self.PBRA_DNS_LOG_UNTRUSTED is False:
            pass
        elif alias and query.reputation_resolver is not None:
            # Register a trusted event
            query.reputation_resolver.event_trusted()
        elif query.transport == 'tcp' and query.reputation_resolver is not None:
            # Register a trusted event
            query.reputation_resolver.event_trusted()
        elif query.transport == 'udp' and query.reputation_resolver is not None:
            # Register an untrusted event
            query.reputation_resolver.event_untrusted()

    @asyncio.coroutine
    def pbra_dns_preprocess_rgw_wan_soa(self, query, addr, host_obj, service_data):
        """ This function implements section: Tackling real resolutions and reputation for remote server(s) and DNS clusters """
        # TODO: Add a case when reputation is very high and UDP.cookie is found for resolver ?
        # TODO: Implement TCPCNAME or CNAME based on reputation of the sender?
        # TODO: Specify the event logging sequence, when do neutral, trusted and untrusted

        fqdn = format(query.question[0].name)
        alias = service_data['alias']

        self._logger.debug('WAN SOA pre-process for {} / {}'.format(fqdn, service_data))

        # Load available reputation metadata in query object
        self._load_metadata_resolver(query, addr, create=self.PBRA_DNS_LOG_UNTRUSTED)
        self._load_metadata_requestor(query, addr, create=False)

        # Log untrusted requests
        self._dns_preprocess_rgw_wan_soa_event_logging(query, alias)


        # Evaluate pre-conditions

        # Anti spoofing mechanisms are not enabled, continue with query processing
        if (self.PBRA_DNS_POLICY_TCPCNAME, self.PBRA_DNS_POLICY_TCP, self.PBRA_DNS_POLICY_CNAME) == (False, False, False):
            self._logger.debug('Anti spoofing mechanisms are not enabled, continue with query processing')
            return None

        ## Enforce PBRA_DNS_POLICY_TCPCNAME
        if query.transport == 'udp' and self.PBRA_DNS_POLICY_TCPCNAME:
            ## Create truncated response
            response = self._policy_tcp(query)
            self._logger.debug('Create TRUNCATED response / {}'.format(service_data))
            return response

        ## Enforce PBRA_DNS_POLICY_TCP
        ### Applies to UDP queries only
        if query.transport == 'udp' and self.PBRA_DNS_POLICY_TCP and alias is False:
            # Ensure spoofed-free communications by triggering TCP requests
            ## Create truncated response
            response = self._policy_tcp(query)
            self._logger.debug('Create TRUNCATED response / {}'.format(service_data))
            return response

        # Continue processing with *trusted* DNS query
        #self._logger.debug('WAN SOA detected trusted query for {} / {}'.format(fqdn, service_data))

        ## Enforce PBRA_DNS_POLICY_CNAME
        if self.PBRA_DNS_POLICY_CNAME is False:
            self._logger.debug('CNAME policy not enabled / {}'.format(service_data))
            return None

        if self.PBRA_DNS_POLICY_CNAME and alias is False:
            # Create CNAME response
            response, _fqdn = self._policy_cname(query)
            # Register alias service in host
            alias_service_data = self._register_host_alias(host_obj, service_data, fqdn, _fqdn)
            ## Create uDNSQueryTimer object
            timer_obj = uDNSQueryTimer(query, addr[0], service_data, alias_service_data)
            # Monkey patch delete function for timer object
            timer_obj.delete = functools.partial(self._cb_dnstimer_deleted, timer_obj, host_obj)
            self.add(timer_obj)

            # Evaluate resolver metadata and create new if does not exist
            if query.reputation_resolver is None:
                # Create reputation metadata in query object
                self._load_metadata_resolver(query, addr, create=True)

            self._logger.debug('Create CNAME response / {}'.format(alias_service_data))
            # Return CNAME response
            return response

        # Query is trusted, load/create metadata related to requestor
        self._load_metadata_requestor(query, addr, create=True)

        # Evaluate seen IP addresses for current FQDN
        timer_obj = self.get((KEY_TIMER_FQDN, fqdn))
        if timer_obj.ipaddr not in query.reputation_resolver.nodes:
            # Merge DNS groups
            group1 = query.reputation_resolver
            group2 = self.get((KEY_DNSGROUP_IPADDR, timer_obj.ipaddr))
            self._coalesce_dns_groups(group1, group2)

        return None

    def _coalesce_dns_groups(self, group1, group2):
        """ Merge two existing DNS groups and update existing DNS host NCID if needed """
        # Merge groups, nodes and calculate new reputation values
        group1.merge(group2)
        self.remove(group2)
        # Update coalesced group keys with new nodes
        self.updatekeys(group1)
        # TODO: Implement update of DNSHosts identified by NCID in DNSGroup
        ## > This requires DNSHost created with NCID to be linked to DNSGroup id


    @asyncio.coroutine
    def pbra_dns_process_rgw_wan_soa(self, query, addr, host_obj, service_data, host_ipv4):
        fqdn = format(query.question[0].name)
        rdtype = query.question[0].rdtype

        # Get cached record
        allocated_ipv4 = self._rgw_cache_get(fqdn, rdtype)
        if allocated_ipv4 is not None:
            self._logger.critical('Using cached result {} ({}) / {}'.format(fqdn, dns.rdatatype.to_text(rdtype), allocated_ipv4))
            return allocated_ipv4

        # Evaluate host data service and use appropriate address pool
        if service_data['proxy_required'] is True:
            # Resolve via Service Pool
            self._logger.debug('Process {} with ServicePool ({}) / {}'.format(fqdn, dns.rdatatype.to_text(rdtype), service_data))
            allocated_ipv4 =  self._rgw_allocate_servicepool()
        else:
            # Resolve via Circular Pool
            self._logger.debug('Process {} with CircularPool ({}) for {} / {}'.format(fqdn, dns.rdatatype.to_text(rdtype), host_ipv4, service_data))
            # Decision making based on load level(s) and reputation
            allocated_ipv4 = yield from self._rgw_allocate_circularpool(query, addr, host_obj, service_data, host_ipv4)

        # Create cached record
        self._rgw_cache_set(fqdn, rdtype, allocated_ipv4)
        return allocated_ipv4

    def _rgw_cache_get(self, fqdn, rdtype):
        """ Return an existing cached rdata for an fqdn/rdtype """
        timer_obj = self.lookup((KEY_TIMER_FQDN, fqdn))
        if timer_obj is None:
            # PBRA_DNS_POLICY_CNAME must not be enabled...
            return None
        if rdtype not in timer_obj.cache:
            # A record has not been allocated
            return None

        # Return cached record
        allocated_ipv4 = timer_obj.cache[rdtype]
        return allocated_ipv4

    def _rgw_cache_set(self, fqdn, rdtype, rdata):
        """ Create a cached rdata for an fqdn/rdtype """
        timer_obj = self.lookup((KEY_TIMER_FQDN, fqdn))
        if timer_obj is None:
            # PBRA_DNS_POLICY_CNAME must not be enabled...
            return None

        # Set active flag to True to indicate use
        timer_obj.active = True

        if rdata is None:
            # A record has not been allocated
            return

        self._logger.debug('Create cached result {} ({}) / {}'.format(fqdn, dns.rdatatype.to_text(rdtype), rdata))
        timer_obj.cache[rdtype] = rdata

    def _rgw_allocate_servicepool(self):
        """ Takes in a DNS query for ServicePool """
        ap_spool = self.pooltable.get('servicepool')
        allocated_ipv4 = ap_spool.release(ap_spool.allocate())
        return allocated_ipv4

    @asyncio.coroutine
    def _rgw_allocate_circularpool(self, query, addr, host_obj, service_data, host_ipv4):
        """ Takes in a DNS query for CircularPool """
        # TODO: Implement logic for policy and reputation checking

        # Get Circular Pool policy for host
        host_policy = host_obj.get_service('CIRCULARPOOL')

        # Update table and remove expired connections
        self.connectiontable.update_all_rgw()

        # Get host usage stats of the pool - lookup because there could be none
        rgw_conns = self.connectiontable.stats(connection.KEY_RGW)
        host_conns = self.connectiontable.stats((connection.KEY_RGW, host_obj.fqdn)) # Use host fqdn as connection id

        # Evaluate host policy for quick exit
        if host_conns >= host_policy['max']:
            self._logger.warning('RealmGateway host policy exceeded: {} pending connection(s)'.format(host_policy['max']))
            return None

        # Calculate system load and find executing policy function
        ## Get Circular Pool address pool stats
        ap_cpool = self.pooltable.get('circularpool')
        pool_size, pool_allocated, pool_available = ap_cpool.get_stats()
        ## Calculate current load in 100%
        sysload = (pool_allocated / pool_size) * 100
        # Enforce Circular Pool policy to allocate an address
        allocated_ipv4 = yield from self._unified_policy_circularpool(query, addr, host_obj, service_data, host_ipv4, sysload)
        return allocated_ipv4

    def _policy_get_query_reputation(self, query, math):
        """ Return the maximum reputation value among DNS resolver and requestor """
        r_resolver = 0
        r_requestor = 0
        if query.reputation_resolver:
            r_resolver = query.reputation_resolver.reputation
        if query.reputation_requestor:
            r_resolver = query.reputation_requestor.reputation
        # Validate choice for math operation
        assert math in ('max', 'min', 'avg')
        if math == 'max':
            return max(r_resolver, r_requestor)
        elif math == 'min':
            return min(r_resolver, r_requestor)
        elif math == 'avg':
            return (r_resolver + r_requestor) / 2.0

    @asyncio.coroutine
    def _unified_policy_circularpool(self, query, addr, host_obj, service_data, host_ipv4, sysload):
        # Obtain load policy parameters
        if self.PBRA_DNS_LOAD_POLICING is False:
            load_policy = {'threshold': -1, 'fqdn_new': 0.0, 'sfqdn_new': 0.0, 'sfqdn_reuse': 0.0, 'math': 'max'}
            self._logger.debug('System load at {:.2f}%% / Best effort allocation'.format(sysload))
        else:
            load_policy = [entry for entry in self.SYSTEM_LOAD if sysload>=entry['threshold']].pop(0)
            self._logger.debug('System load at {:.2f}%% / Using policy {}'.format(sysload, load_policy))

        # Calculate values for policy match
        reputation = self._policy_get_query_reputation(query, load_policy['math'])
        fqdn, sfqdn_reuse = self._describe_service_data(service_data, partial_reuse=False)
        sfqdn      = not fqdn

        # 1. Minimum reputation is required for allocating a new IP address for an FQDN service
        if fqdn and reputation >= load_policy['fqdn_new']:
            self._logger.info('load={:.2f}%% allocation={} reputation={:.2f}/{:.2f}'.format(sysload, 'fqdn_new', reputation, load_policy['fqdn_new']))
            allocated_ipv4 = yield from self._best_effort_allocate(query, addr, host_obj, service_data, host_ipv4)
            return allocated_ipv4

        # 2. Minimum reputation is required for allocating a new IP address for an SFQDN service
        elif sfqdn and reputation >= load_policy['sfqdn_new'] and sfqdn_reuse is False:
            self._logger.info('load={:.2f}%% allocation={} reputation={:.2f}/{:.2f}'.format(sysload, 'sfqdn_new', reputation, load_policy['sfqdn_new']))
            allocated_ipv4 = yield from self._best_effort_allocate(query, addr, host_obj, service_data, host_ipv4)
            return allocated_ipv4

        # 3. Minimum reputation is required for overloading an existing IP address for an SFQDN service
        elif sfqdn and reputation >= load_policy['sfqdn_reuse'] and sfqdn_reuse is True:
            self._logger.info('load={:.2f}%% allocation={} reputation={:.2f}/{:.2f}'.format(sysload, 'sfqdn_reuse', reputation, load_policy['sfqdn_reuse']))
            allocated_ipv4 = yield from self._best_effort_allocate(query, addr, host_obj, service_data, host_ipv4)
            return allocated_ipv4

        # Fine-grained logging of policy violation
        elif fqdn:
            self._logger.warning('Policy violation! load={:.2f}%% allocation={} reputation={:.2f}/{:.2f}'.format(sysload, 'fqdn_new', reputation, load_policy['fqdn_new']))
        elif sfqdn and not sfqdn_reuse:
            self._logger.warning('Policy violation! load={:.2f}%% allocation={} reputation={:.2f}/{:.2f}'.format(sysload, 'sfqdn_new', reputation, load_policy['sfqdn_new']))
        elif sfqdn and sfqdn_reuse:
            self._logger.warning('Policy violation! load={:.2f}%% allocation={} reputation={:.2f}/{:.2f}'.format(sysload, 'sfqdn_reuse', reputation, load_policy['sfqdn_reuse']))


    def _policy_get_max_reputation(self, query):
        """ Return the maximum reputation value among DNS resolver and requestor """
        r_resolver = 0
        r_requestor = 0
        if query.reputation_resolver:
            r_resolver = query.reputation_resolver.reputation
        if query.reputation_requestor:
            r_resolver = query.reputation_requestor.reputation
        return max(r_resolver, r_requestor)

    def _policy_get_min_reputation(self, query):
        """ Return the minimum reputation value among DNS resolver and requestor """
        r_resolver = 0
        r_requestor = 0
        if query.reputation_resolver:
            r_resolver = query.reputation_resolver.reputation
        if query.reputation_requestor:
            r_resolver = query.reputation_requestor.reputation
        return min(r_resolver, r_requestor)

    @asyncio.coroutine
    def _best_effort_allocate(self, query, addr, host_obj, service_data, host_ipv4):
        # TODO: Improve connection creation to include DNS metadata and check for SLA
        # TODO: Define a connection KEY when creating the object to indicate what tuples need to be registered?

        # Obtain FQDN from query
        fqdn = format(query.question[0].name)

        # Get Circular Pool address pool stats
        ap_cpool = self.pooltable.get('circularpool')
        pool_size, pool_allocated, pool_available = ap_cpool.get_stats()

        # Get list of reusable addresses
        reuse_ipaddr_l = self._connection_circularpool_get_overloadable(service_data)
        if len(reuse_ipaddr_l) > 0:
            # Use first available address from the pool
            allocated_ipv4 = reuse_ipaddr_l[0]
            self._logger.debug('Found {} IP(s) for reuse: {}'.format(len(reuse_ipaddr_l), reuse_ipaddr_l))
            self._logger.info('Overloading reserved address: {} @ {}'.format(fqdn, allocated_ipv4))
        elif pool_available > 0:
            # Allocate a new address from the pool
            allocated_ipv4 = ap_cpool.allocate()
        else:
            self._logger.warning('Failed to allocate a new address from CircularPool: {} @ N/A'.format(fqdn))
            return None

        # TODO: Improve dns_host representation and consider the use within connection object (contains())

        dns_resolver = addr[0]
        dns_host = query.reputation_requestor
        dns_bind = False
        # Create DNS bound connection if all parameters are available
        if query.reputation_resolver is None or dns_host is None:
            dns_bind = False
        elif query.reputation_resolver.sla and dns_host.ipaddr:
            dns_bind = True

        # Continue to creating the connection
        # Create RealmGateway connection
        conn_param = {'private_ip': host_ipv4,
                      'private_port': service_data['port'],
                      'outbound_ip': allocated_ipv4,
                      'outbound_port': service_data['port'],
                      #'remote_ip': remote_ip,
                      #'remote_port': remote_port,
                      'protocol': service_data['protocol'],
                      'fqdn': fqdn,
                      'host_fqdn': host_obj.fqdn,
                      'dns_resolver': dns_resolver,
                      'dns_host': dns_host,
                      'dns_bind': dns_bind,
                      'loose_packet': service_data.setdefault('loose_packet', 0),
                      #'autobind': service_data.setdefault('autobind', True),
                      #'timeout': service_data.setdefault('timeout', 0),
                      'query': query
                      }

        conn = ConnectionLegacy(**conn_param)
        # Monkey patch delete function as a coroutine for the connection object
        conn.delete = functools.partial(asyncio.ensure_future, self._cb_connection_deleted(conn))
        # Add connection to table
        self.connectiontable.add(conn)
        # Log
        self._logger.info('Allocated IP address from Circular Pool: {} @ {} for {:.3f} msec'.format(fqdn, allocated_ipv4, conn.timeout*1000))
        self._logger.debug('New Circular Pool connection: {}'.format(conn))

        # Synchronize connection with SYNPROXY module
        ## TODO: Get TCP options policy from host
        if service_data['protocol'] in [0, 6]:
            # TODO: Test performance and consider optimizations / Do this in parallel or yield from it?
            tcpmss, tcpsack, tcpwscale = 1460, 1, 7
            yield from self.network.synproxy_add_connection(conn.outbound_ip, conn.outbound_port, conn.protocol, tcpmss, tcpsack, tcpwscale)

        # Return the allocated address
        return allocated_ipv4

    @asyncio.coroutine
    def _cb_connection_deleted(self, conn):
        # Get Circular Pool address pool
        ap_cpool = self.pooltable.get('circularpool')
        ipaddr = conn.outbound_ip

        if conn.hasexpired():
            # Connection expired
            self._logger.warning('Connection expired: {} in {:.3f} msec '.format(conn, conn.age*1000))
            # Blame attribution to DNS resolver and requestor
            self._logger.debug('  >> Blame attribution!')
            # Register a nok event
            if conn.query.reputation_resolver is not None:
                conn.query.reputation_resolver.event_nok()
            if conn.query.reputation_requestor is not None:
                conn.query.reputation_requestor.event_nok()
        else:
            # Connection was used
            self._logger.debug('Connection used: {} in {:.3f} msec '.format(conn, conn.age*1000))
            # Success attribution to DNS resolver and requestor
            self._logger.debug('  >> Success attribution!')
            ## Register an ok event
            if conn.query.reputation_resolver is not None:
                conn.query.reputation_resolver.event_ok()
            if conn.query.reputation_requestor is not None:
                conn.query.reputation_requestor.event_ok()

        # Synchronize connection with SYNPROXY module
        if (conn.outbound_port, conn.protocol) == (0 ,0):
            # This is an FQDN connection -> Reset TCP default options in SYNPROXY connection!
            tcpmss, tcpsack, tcpwscale = 1460, 1, 7
            yield from self.network.synproxy_add_connection(conn.outbound_ip, conn.outbound_port, conn.protocol, tcpmss, tcpsack, tcpwscale)
        elif conn.protocol in [0, 6]:
            # This is an (S)FQDN connection -> Remove from SYNPROXY!
            yield from self.network.synproxy_del_connection(conn.outbound_ip, conn.outbound_port, conn.protocol)

        # Get RealmGateway connections
        if self.connectiontable.has((connection.KEY_RGW, ipaddr)):
            self._logger.debug('Cannot release IP address to Circular Pool: {} @ {} still in use for {:.3f} msec'.format(conn.fqdn, ipaddr, conn.age*1000))
            return

        ap_cpool.release(ipaddr)
        self._logger.info('Released IP address to Circular Pool: {} @ {} in {:.3f} msec'.format(ipaddr, conn.fqdn, conn.age*1000))

    def _describe_service_data(self, service_data, partial_reuse=True):
        """ Return fqdn, sfqdn_reuse booleans according to service_data definition """
        if service_data['port'] == 0 and service_data['protocol'] == 0:
            fqdn = True
            sfqdn_reuse = False
        elif service_data['port'] != 0 and service_data['protocol'] != 0:
            fqdn = False
            sfqdn_reuse = True
        elif (service_data['port'] != 0 or service_data['protocol'] != 0) and partial_reuse:
            fqdn = False
            sfqdn_reuse = True
        else:
            fqdn = False
            sfqdn_reuse = False
        return (fqdn, sfqdn_reuse)

    def _connection_circularpool_get_overloadable(self, service_data):
        """ Returns a list of IPv4 address that can be overloaded """
        port, protocol = service_data['port'], service_data['protocol']
        self._logger.debug('Attempt to overload connection for {}:{}'.format(port, protocol))
        # List of available addresses for reuse
        available = []

        if not self.connectiontable.has(connection.KEY_RGW):
            return available

        # Iterate all RealmGateway connections and try to reuse existing allocated IP addresses
        rgw_conns = self.connectiontable.get(connection.KEY_RGW)
        for conn in rgw_conns:
            ipaddr = conn.outbound_ip
            c_port, c_proto = conn.outbound_port, conn.protocol
            s_port, s_proto = port, protocol

            # Do not iterate already available addresses
            if ipaddr in available:
                continue

            self._logger.debug('Comparing {} vs {} @{}'.format((c_port, c_proto),(s_port, s_proto), ipaddr))
            # The following statements match when IP overloading cannot be performed
            if (c_port == 0 and c_proto == 0) or (s_port == 0 and s_proto == 0):
                self._logger.debug('0. Port & Protocol blocked')
                continue
            elif (c_port == s_port) and (c_proto == s_proto or c_proto == 0 or s_proto == 0):
                self._logger.debug('1. Port blocked')
                continue
            elif (c_proto == s_proto) and (c_port == 0 or s_port == 0):
                self._logger.debug('2. Port blocked')
                continue

            available.append(ipaddr)
        # Return list of available IP addresses for overload
        return available

    def _register_host_alias(self, host_obj, service_data, original_fqdn, alias_fqdn):
        # Add alias as SFQDN host service
        #service_data = {'fqdn':'foo.', 'port':0, 'protocol':0, 'proxy_required':False, 'carriergrade':False}
        _service_data = dict(service_data)
        _service_data['_fqdn'] = original_fqdn
        _service_data['fqdn'] = alias_fqdn
        _service_data['alias'] = True
        # Add alias service to host_obj
        host_obj.add_service(KEY_SERVICE_SFQDN, _service_data)
        # Update lookup keys in host table
        self.hosttable.updatekeys(host_obj)
        # Return newly created service_data
        return _service_data

    def _cb_dnstimer_deleted(self, timer_obj, host_obj):
        # Update reputation values based on timer_obj utilization. DNS group must exists
        dnsgroup_obj = self.get((KEY_DNSGROUP_IPADDR, timer_obj.ipaddr))
        if not timer_obj.active:
            # Timer could not be used. Do not blame any party
            self._logger.info('[--] Timer expired {}'.format(timer_obj))
            pass
        elif len(timer_obj.cache):
            self._logger.debug('[OK] Timer expired {}'.format(timer_obj))
            dnsgroup_obj.event_ok()
        else:
            self._logger.warning('[KO] Timer expired {}'.format(timer_obj))
            dnsgroup_obj.event_nok()

        # Remove alias FQDN service from host
        host_obj.remove_service(KEY_SERVICE_SFQDN, timer_obj.alias_service)
        # Update lookup keys in host table
        self.hosttable.updatekeys(host_obj)


    def pbra_data_preaccept_circularpool(self, data, packet_fields):
        # Check if the endpoints are known
        key = (KEY_DATA_PACKET, (packet_fields['src'], packet_fields['dst']))
        if not self.has(key):
            # We have not seent this packet before
            return True

        node = self.get(key)
        if not node.has_record(**packet_fields):
            # We have not seent this packet before
            return True

        record = node.get_record(**packet_fields)
        self._logger.debug('Found prior state: {} / {}'.format(packet_fields, record))
        return False

    def pbra_data_track_circularpool(self, data, packet_fields):
        # Create a record for a seen packet
        key = (KEY_DATA_PACKET, (packet_fields['src'], packet_fields['dst']))
        if not self.has(key):
            node = uStateDataPacket(packet_fields['src'], packet_fields['dst'])
            self.add(node)
        else:
            node = self.get(key)

        node.add_record(**packet_fields)
        self._logger.debug('Tracking packet: {}'.format(packet_fields))


def _do_ok(obj, n):
    for _ in range(n):
        obj.event_ok()
        print(obj.reputation)

def _do_nok(obj, n):
    for _ in range(n):
        obj.event_nok()
        print(obj.reputation)

def _do_neutral(obj, n):
    for _ in range(n):
        obj.event_neutral()
        print(obj.reputation)

if __name__ == '__main__':
    obj1 = uReputation()
    #_do_neutral(obj1, 5)
    _do_ok(obj1, 100)
    #_do_nok(obj1, 1)

