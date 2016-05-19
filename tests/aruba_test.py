# Copyright 2014 Google Inc. All Rights Reserved.
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

"""Unittest for Aruba acl rendering module."""

import unittest

from lib import aruba
from lib import nacaddr
from lib import naming
from lib import policy
import mox

GOOD_HEADER_IPV4 = """
header {
  comment:: "For QoS classification of servers on Aruba controllers."
  target:: aruba SERVER-LIST ipv4
}
"""

GOOD_TERM_IPV4 = """
term good-term-1 {
  comment:: "All servers."
  address:: SERVERS
  action:: accept
}
"""

BAD_TERM_IPV4 = """
term good-term-1 {
  comment:: "All servers."
  address:: SERVERS
  action:: deny
}
"""

GOOD_HEADER_IPV6 = """
header {
  comment:: "For QoS classification of IPv6 servers on Aruba controllers."
  target:: aruba SERVER-LIST_6 ipv6
}
"""

GOOD_TERM_IPV6 = """
term good-term-1 {
  comment:: "All servers."
  address:: SERVERS
  action:: accept
}
"""

# Print a info message when a term is set to expire in that many weeks.
# This is normally passed from command line.
EXP_INFO = 2

TEST_IPS = [nacaddr.IP('10.2.3.4/32'),
            nacaddr.IP('2001:4860:8000::5/128')]


class ArubaTest(unittest.TestCase):

  def setUp(self):
    self.mox = mox.Mox()
    self.naming = self.mox.CreateMock(naming.Naming)

  def tearDown(self):
    self.mox.VerifyAll()
    self.mox.UnsetStubs()
    self.mox.ResetAll()

  def testNetdestination(self):
    self.naming.GetNetAddr('SERVERS').AndReturn(TEST_IPS)
    self.mox.ReplayAll()
    acl = aruba.Aruba(policy.ParsePolicy(
        GOOD_HEADER_IPV4 + GOOD_TERM_IPV4, self.naming), EXP_INFO)
    self.assertTrue('netdestination SERVER-LIST' in str(acl))
    self.assertTrue('  host 10.2.3.4' in str(acl))
    self.assertFalse('  host 2001:4860:8000::5' in str(acl))

  def testNetdestination6(self):
    self.naming.GetNetAddr('SERVERS').AndReturn(TEST_IPS)
    self.mox.ReplayAll()
    acl = aruba.Aruba(policy.ParsePolicy(
        GOOD_HEADER_IPV6 + GOOD_TERM_IPV6, self.naming), EXP_INFO)
    self.assertTrue('netdestination6 SERVER-LIST_6' in str(acl))
    self.assertTrue('  host 2001:4860:8000::5' in str(acl))
    self.assertFalse('  host 10.2.3.4' in str(acl))

  def testActionUnsupported(self):
    self.naming.GetNetAddr('SERVERS').AndReturn(TEST_IPS)
    self.mox.ReplayAll()
    self.assertRaisesRegexp(
        aruba.UnsupportedArubaAccessListError,
        'Aruba ACL action must be "accept".',
        aruba.Aruba,
        policy.ParsePolicy(
            GOOD_HEADER_IPV4 + BAD_TERM_IPV4, self.naming),
        EXP_INFO)


if __name__ == '__main__':
  unittest.main()
