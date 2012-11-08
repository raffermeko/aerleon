#!/usr/bin/python
#
# Copyright 2010 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Iptables generator."""

__author__ = 'watson@google.com (Tony Watson)'

import aclgenerator
import datetime
import logging
import nacaddr
import re


class Term(aclgenerator.Term):
  """Generate Iptables policy terms."""

  # Validate that term does not contain any fields we do not
  # support.  This prevents us from thinking that our output is
  # correct in cases where we've omitted fields from term.
  _PLATFORM = 'iptables'
  _POSTJUMP_FORMAT = None
  _PREJUMP_FORMAT = '-A %s -j %s'
  _ACTION_TABLE = {
      'accept': '-j ACCEPT',
      'deny': '-j DROP',
      'reject': '-j REJECT --reject-with icmp-host-prohibited',
      'reject-with-tcp-rst': '-j REJECT --reject-with tcp-reset',
      'next': '-j RETURN'
      }
  _PROTO_TABLE = {
      'icmpv6': '-p icmpv6',
      'icmp': '-p icmp',
      'tcp': '-p tcp',
      'udp': '-p udp',
      'all': '-p all',
      'esp': '-p esp',
      'ah': '-p ah',
      'gre': '-p gre',
      }
  _TCP_FLAGS_TABLE = {
      'syn': 'SYN',
      'ack': 'ACK',
      'fin': 'FIN',
      'rst': 'RST',
      'urg': 'URG',
      'psh': 'PSH',
      'all': 'ALL',
      'none': 'NONE',
      }
  _KNOWN_OPTIONS_MATCHERS = {
      # '! -f' also matches non-fragmented packets.
      'first-fragment': '-m u32 --u32 4&0x3FFF=0x2000',
      'initial': '--syn',
      'tcp-initial': '--syn',
      'sample': '',
      }

  def __init__(self, term, filter_name, trackstate, filter_action, af='inet',
               truncate=True):
    """Setup a new term.

    Args:
      term: A policy.Term object to represent in iptables.
      filter_name: The name of the filter chan to attach the term to.
      trackstate: Specifies if conntrack should be used for new connections
      filter_action: The default action of the filter.
      af: Which address family ('inet' or 'inet6') to apply the term to.
      truncate: Whether to truncate names to meet iptables limits.

    Raises:
      UnsupportedFilterError: Filter is not supported.
    """
    self.trackstate = trackstate
    self.term = term  # term object
    self.filter = filter_name  # actual name of filter
    self.default_action = filter_action
    self.options = []
    self.af = af

    # Iptables enforces 30 char limit, but weirdness happens after 28 or 29
    self.term_name = '%s_%s' % (
        self.filter[:1], self._CheckTermLength(self.term.name, 24, truncate))

    if af == 'inet6':
      self._all_ips = nacaddr.IPv6('::/0')
      self._ACTION_TABLE['reject'] = '-j REJECT --reject-with adm-prohibited'
    else:
      self._all_ips = nacaddr.IPv4('0.0.0.0/0')
      self._ACTION_TABLE['reject'] = '-j REJECT --reject-with ' \
                                     'icmp-host-prohibited'

  def __str__(self):
    # Verify platform specific terms. Skip whole term if platform does not
    # match.
    if self.term.platform:
      if self._PLATFORM not in self.term.platform:
        return ''
    if self.term.platform_exclude:
      if self._PLATFORM in self.term.platform_exclude:
        return ''

    ret_str = []

    # Don't render icmpv6 protocol terms under inet, or icmp under inet6
    if ((self.af == 'inet6' and 'icmp' in self.term.protocol) or
        (self.af == 'inet' and 'icmpv6' in self.term.protocol)):
      ret_str.append('# Term %s' % self.term.name)
      ret_str.append('# not rendered due to protocol/AF mismatch.')
      return '\n'.join(ret_str)

    # Term verbatim output - this will skip over most normal term
    # creation code by returning early. Warnings provided in policy.py
    if self.term.verbatim:
      for next_verbatim in self.term.verbatim:
        if next_verbatim.value[0] == self._PLATFORM:
          ret_str.append(str(next_verbatim.value[1]))
      return '\n'.join(ret_str)

    # We don't support these keywords for filtering, so unless users
    # put in a "verbatim:: iptables" statement, any output we emitted
    # would misleadingly suggest that we applied their filters.
    # Instead, we fail loudly.
    if self.term.ether_type:
      raise UnsupportedFilterError('\n%s %s %s %s' % (
          'ether_type unsupported by', self._PLATFORM,
          '\nError in term', self.term.name))
    if self.term.address:
      raise UnsupportedFilterError('\n%s %s %s %s %s' % (
          'address unsupported by', self._PLATFORM,
          '- specify source or dest', '\nError in term:', self.term.name))
    if self.term.port:
      raise UnsupportedFilterError('\n%s %s %s %s %s' % (
          'port unsupported by', self._PLATFORM,
          '- specify source or dest', '\nError in term:', self.term.name))

    # Create a new term
    ret_str.append('-N %s' % self.term_name)  # New term

    if self._PREJUMP_FORMAT:
      ret_str.append(self._PREJUMP_FORMAT % (self.filter, self.term_name))

    # reformat long comments, if needed
    #
    # iptables allows individual comments up to 256 chars.
    # But our generator will limit a single comment line to < 120, using:
    # max = 119 - 27 (static chars in comment command) - [length of term name]
    comment_max_width = 92 - len(self.term_name)
    if comment_max_width < 40:
      comment_max_width = 40
    comments = aclgenerator.WrapWords(self.term.comment, comment_max_width)
    # append comments to output
    if comments and comments[0]:
      for line in comments:
        if not line:
          continue  # iptables-restore does not like 0-length comments.
        # term comments
        ret_str.append('-A %s -m comment --comment "%s"' %
                       (self.term_name, str(line)))

    # if terms does not specify action, use filter default action
    if not self.term.action:
      self.term.action[0].value = self.default_action

    # Unsupported configuration; in the case of 'accept' or 'next', we
    # skip the rule.  In other cases, we blow up (raise an exception)
    # to ensure that this is not considered valid configuration.
    if self.term.source_prefix or self.term.destination_prefix:
      if str(self.term.action[0]) not in set(['accept', 'next']):
        raise UnsupportedFilterError('%s %s %s %s %s %s %s %s' % (
            '\nTerm', self.term.name, 'has action', str(self.term.action[0]),
            'with source_prefix or destination_prefix,',
            ' which is unsupported in', self._PLATFORM, 'iptables output.'))
      return ('# skipped %s due to source or destination prefix rule' %
              self.term.name)

    # protocol
    if self.term.protocol:
      protocol = self.term.protocol
    else:
      protocol = ['all']
    if self.term.protocol_except:
      raise UnsupportedFilterError('%s %s %s' % (
          '\n', self.term.name,
          'protocol_except logic not currently supported.'))

    # source address
    term_saddr = self.term.source_address
    exclude_saddr = self.term.source_address_exclude
    term_saddr_excluded = []
    if not term_saddr:
      term_saddr = [self._all_ips]
    if exclude_saddr:
      term_saddr_excluded.extend(nacaddr.ExcludeAddrs(term_saddr,
                                                      exclude_saddr))

    # destination address
    term_daddr = self.term.destination_address
    exclude_daddr = self.term.destination_address_exclude
    term_daddr_excluded = []
    if not term_daddr:
      term_daddr = [self._all_ips]
    if exclude_daddr:
      term_daddr_excluded.extend(nacaddr.ExcludeAddrs(term_daddr,
                                                      exclude_daddr))

    # Just to be safe, always have a result of at least 1 to avoid * by zero
    # returning incorrect results (10src*10dst=100, but 10src*0dst=0, not 10)
    bailout_count = len(exclude_saddr) + len(exclude_daddr) + (
        (len(self.term.source_address) or 1) *
        (len(self.term.destination_address) or 1))
    exclude_count = ((len(term_saddr_excluded) or 1) *
                     (len(term_daddr_excluded) or 1))

    # Use bailout jumps for excluded addresses if it results in fewer output
    # lines than nacaddr.ExcludeAddrs() method.
    if exclude_count < bailout_count:
      exclude_saddr = []
      exclude_daddr = []
      if term_saddr_excluded:
        term_saddr = term_saddr_excluded
      if term_daddr_excluded:
        term_daddr = term_daddr_excluded

    # ports
    source_port = []
    destination_port = []
    if self.term.source_port:
      source_port = self.term.source_port
    if self.term.destination_port:
      destination_port = self.term.destination_port

    # icmp-types
    icmp_types = ['']
    if self.term.icmp_type:
      icmp_types = self.NormalizeIcmpTypes(self.term.icmp_type, protocol,
                                           self.af)

    source_interface = ''
    if self.term.source_interface:
      source_interface = self.term.source_interface

    destination_interface = ''
    if self.term.destination_interface:
      destination_interface = self.term.destination_interface

    log_hits = False
    if self.term.logging:
      # Iptables sends logs to hosts configured syslog
      log_hits = True

    # options
    tcp_flags = []
    tcp_track_options = []
    for next_opt in [str(x) for x in self.term.option]:
      #
      # Sanity checking and high-ports are added as appropriate in
      # pre-processing that is done in __str__ within class Iptables.
      # Option established will add destination port high-ports if protocol
      # contains only tcp, udp or both.  This is done earlier in class Iptables.
      #
      if ((next_opt.find('established') == 0 or
           next_opt.find('tcp-established') == 0)
          and 'ESTABLISHED' not in [x.strip() for x in self.options]):
        if next_opt.find('tcp-established') == 0 and protocol != ['tcp']:
          raise TcpEstablishedError('%s %s %s' % (
              '\noption tcp-established can only be applied for proto tcp.',
              '\nError in term:', self.term.name))

        if self.trackstate:
          # Use nf_conntrack to track state -- works with any proto
          self.options.append('-m state --state ESTABLISHED,RELATED')
        elif protocol == ['tcp']:
          # Simple established-only rule for TCP: Must have ACK field
          # (SYN/ACK or subsequent ACK), or RST and no other flags.
          tcp_track_options = [(['ACK'], ['ACK']),
                               (['SYN', 'FIN', 'ACK', 'RST'], ['RST'])]

      # Iterate through flags table, and create list of tcp-flags to append
      for next_flag in self._TCP_FLAGS_TABLE:
        if next_opt.find(next_flag) == 0:
          tcp_flags.append(self._TCP_FLAGS_TABLE.get(next_flag))
      if next_opt in self._KNOWN_OPTIONS_MATCHERS:
        self.options.append(self._KNOWN_OPTIONS_MATCHERS[next_opt])
    if self.term.packet_length:
      # Policy format is "#-#", but iptables format is "#:#"
      self.options.append('-m length --length %s' %
                          self.term.packet_length.replace('-', ':'))
    if self.term.fragment_offset:
      self.options.append('-m u32 --u32 4&0x1FFF=%s' %
                          self.term.fragment_offset.replace('-', ':'))

    for saddr in exclude_saddr:
      ret_str.extend(self._FormatPart(
          self.af, '', saddr, '', '', '', '', '', '', '', '', '', '',
          self._ACTION_TABLE.get('next')))
    for daddr in exclude_daddr:
      ret_str.extend(self._FormatPart(
          self.af, '', '', '', daddr, '', '', '', '', '', '', '', '',
          self._ACTION_TABLE.get('next')))

    for saddr in term_saddr:
      for daddr in term_daddr:
        for icmp in icmp_types:
          for proto in protocol:
            for tcp_matcher in tcp_track_options or (([], []),):
              ret_str.extend(self._FormatPart(
                  self.af,
                  str(proto),
                  saddr,
                  source_port,
                  daddr,
                  destination_port,
                  self.options,
                  tcp_flags,
                  icmp,
                  tcp_matcher,
                  source_interface,
                  destination_interface,
                  log_hits,
                  self._ACTION_TABLE.get(str(self.term.action[0]))
                  ))

    if self._POSTJUMP_FORMAT:
      ret_str.append(self._POSTJUMP_FORMAT % (self.filter, self.term_name))

    return '\n'.join(str(v) for v in ret_str if v is not '')

  def _FormatPart(self, af, protocol, saddr, sport, daddr, dport, options,
                  tcp_flags, icmp_type, track_flags, sint, dint, log_hits,
                  action):
    """Compose one iteration of the term parts into a string.

    Args:
      af: Address family, inet|inet6
      protocol: The network protocol
      saddr: Source IP address
      sport: Source port numbers
      daddr: Destination IP address
      dport: Destination port numbers
      options: Optional arguments to append to our rule
      tcp_flags: Which tcp_flag arguments, if any, should be appended
      icmp_type: What icmp protocol to allow, if any
      track_flags: A tuple of ([check-flags], [set-flags]) arguments to tcp-flag
      sint: Optional source interface
      dint: Optional destination interface
      log_hits: Boolean, to log matches or not
      action: What should happen if this rule matches
    Returns:
      rval:  A single iptables argument line
    """
    src = ''
    dst = ''
    # Check that AF matches and is what we want
    if saddr:
      if (af == 'inet') and (saddr.version != 4):
        return ''
      if (af == 'inet6') and (saddr.version != 6):
        return ''
    if daddr:
      if (af == 'inet') and (daddr.version != 4):
        return ''
      if (af == 'inet6') and (daddr.version != 6):
        return ''
    filter_top = '-A ' + self.term_name
    # fix addresses
    if not saddr or saddr == self._all_ips:
      src = ''
    else:
      src = '-s %s/%d' % (saddr.ip, saddr.prefixlen)

    if not daddr or daddr == self._all_ips:
      dst = ''
    else:
      dst = '-d %s/%d' % (daddr.ip, daddr.prefixlen)

    source_int = ''
    if sint:
      source_int = '-i %s' % sint

    destination_int = ''
    if dint:
      destination_int = '-o %s' % dint

    log_jump = ''
    if log_hits:
      log_jump = '-j LOG --log-prefix %s ' % self.term.name

    if not options:
      options = []

    proto = self._PROTO_TABLE.get(str(protocol))
    # Don't drop protocol if we don't recognize it
    if protocol and not proto:
      proto = '-p %s' % str(protocol)

    # set conntrack state to NEW, unless policy requested "nostate"
    if self.trackstate:
      already_stateful = False
      # we will add new stateful arguments only if none already exist, such
      # as from "option:: established"
      for option in options:
        if 'state' in option:
          already_stateful = True
      if not already_stateful:
        if 'ACCEPT' in action:
          # We have to permit established/related since a policy may not
          # have an existing blank permit for established/related, which
          # may be more efficient, but slightly less secure.
          options.append('-m state --state NEW,ESTABLISHED,RELATED')

    if tcp_flags or (track_flags and track_flags[0]):
      check_fields = ','.join(set(tcp_flags + track_flags[0]))
      set_fields = ','.join(set(tcp_flags + track_flags[1]))
      flags = '--tcp-flags %s %s' % (check_fields, set_fields)
    else:
      flags = ''

    icmp_type = str(icmp_type)
    if not icmp_type:
      icmp = ''
    elif str(protocol) == 'icmpv6':
      icmp = '--icmpv6-type %s' % icmp_type
    else:
      icmp = '--icmp-type %s' % icmp_type

    # format tcp and udp ports
    sports = dports = ['']
    if sport:
      sports = self._GeneratePortStatement(sport, source=True)
    if dport:
      dports = self._GeneratePortStatement(dport, dest=True)

    ret_lines = []
    for sport in sports:
      for dport in dports:
        rval = [filter_top]
        if re.search('multiport', sport) and not re.search('multiport', dport):
          # Due to bug in iptables, use of multiport module before a single
          # port specification will result in multiport trying to consume it.
          # this is a little hack to ensure single ports are listed before
          # any multiport specification.
          dport, sport = sport, dport
        for value in (proto, flags, sport, dport, icmp, src, dst,
                      ' '.join(options), source_int, destination_int):
          if value:
            rval.append(str(value))
        if log_jump:
          # -j LOG
          ret_lines.append(' '.join(rval+[log_jump]))
        # -j ACTION
        ret_lines.append(' '.join(rval+[action]))
    return ret_lines

  def _GeneratePortStatement(self, ports, source=False, dest=False):
    """Return the 'port' section of an individual iptables rule.

    Args:
      ports: list of ports or port ranges (pairs)
      source: (bool) generate a source port rule
      dest: (bool) generate a dest port rule

    Returns:
      list holding the 'port' sections of an iptables rule.

    Raises:
      BadPortsError: if too many ports are passed in, or if both 'source'
                        and 'dest' are true.
      NotImplementedError: if both 'source' and 'dest' are true.
    """
    if not ports:
      return ''

    direction = ''  # default: no direction / '--port'.  As yet, unused.
    if source and dest:
      raise BadPortsError('_GeneratePortStatement called ambiguously.')
    elif source:
      direction = 's'  # source port / '--sport'
    elif dest:
      direction = 'd'  # dest port / '--dport'
    else:
      raise NotImplementedError('--port support not yet implemented.')

    # Normalize ports and get accurate port count.
    # iptables multiport module limits to 15, but we use 14 to ensure a range
    # doesn't tip us over the limit
    max_ports = 14
    norm_ports = []
    portstrings = []
    count = 0
    for port in ports:
      if port[0] == port[1]:
        norm_ports.append(str(port[0]))
        count += 1
      else:
        norm_ports.append('%d:%d' % (port[0], port[1]))
        count += 2
      if count >= max_ports:
        count = 0
        portstrings.append('-m multiport --%sports %s' % (direction,
                                                          ','.join(norm_ports)))
        norm_ports = []
    if len(norm_ports) == 1:
      portstrings.append('--%sport %s' % (direction, norm_ports[0]))
    else:
      portstrings.append('-m multiport --%sports %s' % (direction,
                                                        ','.join(norm_ports)))
    return portstrings

  def _CheckTermLength(self, term_name, term_max_len, abbreviate):
    """Return a name based on term_name which is shorter than term_max_len.

    Args:
      term_name: A name to abbreviate if necessary.
      term_max_len: An int representing the maximum acceptable length.
      abbreviate: whether to allow abbreviations to shorten the length
    Returns:
      A string based on term_name, abbreviated as necessary to fit term_max_len.
    Raises:
      TermNameTooLongError: term_name cannot be abbreviated below term_max_len.
    """
    # We use uppercase for abbreviations to distinguish from lowercase
    # names.  Ordered list of abbreviations, we try the ones in the
    # top of the list before the ones later in the list.  Prefer clear
    # or very-space-saving abbreviations by putting them early in the
    # list.  Abbreviations may be regular expressions or fixed terms;
    # prefer fixed terms unless there's a clear benefit to regular
    # expressions.
    abbreviation_table = [
        ('bogons', 'BGN'),
        ('bogon', 'BGN'),
        ('reserved', 'RSV'),
        ('rfc1918', 'PRV'),
        ('rfc-1918', 'PRV'),
        ('internet', 'EXT'),
        ('global', 'GBL'),
        ('internal', 'INT'),
        ('customer', 'CUST'),
        ('google', 'GOOG'),
        ('ballmer', 'ASS'),
        ('microsoft', 'LOL'),
        ('china', 'BAN'),
        ('border', 'BDR'),
        ('service', 'SVC'),
        ('router', 'RTR'),
        ('transit', 'TRNS'),
        ('experiment', 'EXP'),
        ('established', 'EST'),
        ('unreachable', 'UNR'),
        ('fragment', 'FRG'),
        ('accept', 'OK'),
        ('discard', 'DSC'),
        ('reject', 'REJ'),
        ('replies', 'ACK'),
        ('request', 'REQ'),
        ]
    new_term = term_name
    if abbreviate:
      for word, abbrev in abbreviation_table:
        if len(new_term) <= term_max_len:
          return new_term
        new_term = re.sub(word, abbrev, new_term)
    if len(new_term) <= term_max_len:
      return new_term
    raise TermNameTooLongError('%s %s %s %s%s %d %s' % (
        '\nTerm', new_term, '(originally', term_name,
        ') is too long. Limit is 24 characters (vs', len(new_term),
        ') and no abbreviations remain.'))


class Iptables(aclgenerator.ACLGenerator):
  """Generates filters and terms from provided policy object."""

  _PLATFORM = 'iptables'
  _DEFAULT_PROTOCOL = 'all'
  _SUFFIX = ''
  _RENDER_PREFIX = None
  _RENDER_SUFFIX = None
  _DEFAULTACTION_FORMAT = '-P %s %s'
  _DEFAULT_ACTION = 'DROP'
  _TERM = Term
  _OPTIONAL_SUPPORTED_KEYWORDS = set(['counter',
                                      'destination_prefix',  # skips these terms
                                      'expiration',
                                      'fragment_offset',
                                      'logging',
                                      'packet_length',
                                      'policer',             # safely ignored
                                      'qos',
                                      'source_interface',
                                      'destination_interface',
                                      'source_prefix',       # skips these terms
                                     ])

  def _TranslatePolicy(self, pol):
    self.iptables_policies = []
    current_date = datetime.date.today()

    default_action = None
    good_default_actions = ['ACCEPT', 'DROP']
    good_filters = ['INPUT', 'OUTPUT', 'FORWARD']
    good_afs = ['inet', 'inet6']
    good_options = ['nostate', 'truncatenames']
    all_protocols_stateful = True

    for header, terms in pol.filters:
      filter_type = None
      if not self._PLATFORM in header.platforms:
        continue

      filter_options = header.FilterOptions(self._PLATFORM)[1:]
      filter_name = header.FilterName(self._PLATFORM)

      if filter_name not in good_filters:
        logging.warn('Filter is generating a non-standard chain that will not '
                     'apply to traffic unless linked from INPUT, OUTPUT or '
                     'FORWARD filters. New chain name is: %s', filter_name)

      # ensure all options after the filter name are expected
      for opt in filter_options:
        if opt not in good_default_actions + good_afs + good_options:
          raise UnsupportedTargetOption('%s %s %s %s' % (
              '\nUnsupported option found in', self._PLATFORM,
              'target definition:', opt))

      # disable stateful?
      if 'nostate' in filter_options:
        all_protocols_stateful = False

      # Check for matching af
      for address_family in good_afs:
        if address_family in filter_options:
          # should not specify more than one AF in options
          if filter_type is not None:
            raise UnsupportedFilterError('%s %s %s %s' % (
                '\nMay only specify one of', good_afs, 'in filter options:',
                filter_options))
          filter_type = address_family
      if filter_type is None:
        filter_type = 'inet'

      if self._PLATFORM == 'iptables' and filter_name == 'FORWARD':
        default_action = 'DROP'

      # does this policy override the default filter actions?
      for next_target in header.target:
        if next_target.platform == self._PLATFORM:
          if len(next_target.options) > 1:
            for arg in next_target.options:
              if arg in good_default_actions:
                default_action = arg
      if default_action and default_action not in good_default_actions:
        raise UnsupportedDefaultAction('%s %s %s %s %s' % (
            '\nOnly', ', '.join(good_default_actions),
            'default filter action allowed;', default_action, 'used.'))

      # add the terms
      new_terms = []
      term_names = set()
      for term in terms:
        if term.name in term_names:
          raise aclgenerator.DuplicateTermError(
              'You have a duplicate term: %s' % term.name)
        term_names.add(term.name)

        term = self.FixHighPorts(term, af=filter_type,
                                 all_protocols_stateful=all_protocols_stateful)
        if not term:
          continue

        if term.expiration and term.expiration <= current_date:
          logging.warn('WARNING: Term %s in policy %s is expired and will '
                       'not be rendered.', term.name, filter_name)
          continue

        new_terms.append(self._TERM(term, filter_name, all_protocols_stateful,
                                    default_action, filter_type,
                                    'truncatenames' in filter_options))

      self.iptables_policies.append((header, filter_name, filter_type,
                                     default_action, new_terms))

  def __str__(self):
    target = []
    pretty_platform = '%s%s' % (self._PLATFORM[0].upper(), self._PLATFORM[1:])

    if self._RENDER_PREFIX:
      target.append(self._RENDER_PREFIX)

    for (header, filter_name, filter_type, default_action, terms
        ) in self.iptables_policies:
      # Add comments for this filter
      target.append('# %s %s Policy' % (pretty_platform,
                                        header.FilterName(self._PLATFORM)))

      # reformat long text comments, if needed
      comments = aclgenerator.WrapWords(header.comment, 70)
      if comments and comments[0]:
        for line in comments:
          target.append('# %s' % line)
        target.append('#')
      # add the p4 tags
      target.extend(aclgenerator.AddRepositoryTags('# '))
      target.append('# ' + filter_type)

      # always specify the default filter states for speedway,
      # if default action policy not specified for iptables, do nothing.
      if self._PLATFORM == 'speedway':
        if not default_action:
          target.append(self._DEFAULTACTION_FORMAT % (filter_name,
                                                      self._DEFAULT_ACTION))
      if default_action:
        target.append(self._DEFAULTACTION_FORMAT % (filter_name,
                                                    default_action))
      # add the terms
      for term in terms:
        target.append(str(term))
      target.append('\n')

    if self._RENDER_SUFFIX:
      target.append(self._RENDER_SUFFIX)

    return '\n'.join(target)


class Error(Exception):
  """Base error class."""


class TermNameTooLongError(Error):
  """Term name is too long."""


class BadPortsError(Error):
  """Too many ports for a single iptables statement."""


class UnsupportedFilterError(Error):
  """Raised when we see an inappropriate filter."""


class NoIptablesPolicyError(Error):
  """Raised when a policy is received that doesn't support iptables."""


class TcpEstablishedError(Error):
  """Raised when a term has tcp-established option but not proto tcp only."""


class EstablishedError(Error):
  """Raised when a term has established option with inappropriate protocol."""


class UnsupportedDefaultAction(Error):
  """Raised when a filter has an impermissible default action specified."""


class UnsupportedTargetOption(Error):
  """Raised when a filter has an impermissible default action specified."""
