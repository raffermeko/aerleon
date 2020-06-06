# Lint as: python3
"""Generic Google Cloud Platform multi-product generator.

Base class for GCP firewalling products.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import json
import re

from capirca.lib import aclgenerator

import six


class Error(Exception):
  """Generic error class."""


class TermError(Error):
  """Raised when a term is not valid."""


class HeaderError(Error):
  """Raised when a header is not valid."""


class UnsupportedFilterTypeError(Error):
  """Raised when an unsupported filter type is specified."""


class Term(aclgenerator.Term):
  """A Term object."""

  def _TruncateComment(self, max_length):
    """Truncate comment."""
    raw_comment = ' '.join(self.term.comment)
    if len(raw_comment) > max_length:
      return raw_comment[:max_length]
    return raw_comment

  def _GetPorts(self):
    """Return a port or port range in string format."""
    ports = []
    for start, end in self.term.destination_port:
      if start == end:
        ports.append(str(start))
      else:
        ports.append('%d-%d' % (start, end))
    return ports

  def _GetLoggingSetting(self):
    """Return true if a term indicates that logging is desired."""
    # Supported values in GCP are '', 'true', and 'True'.
    if self.term.logging in ['true', 'True']:
      return True
    return False


class GCP(aclgenerator.ACLGenerator):
  """A GCP object."""

  policies = []
  _GOOD_DIRECTION = ['INGRESS', 'EGRESS']

  def __str__(self):
    """Return the JSON blob for a GCP object."""
    out = '%s\n\n' % (
        json.dumps(self.policies, indent=2,
                   separators=(six.ensure_str(','), six.ensure_str(': ')),
                   sort_keys=True))
    return out


def IsDefaultDeny(term):
  """Return true if a term is a default deny without IPs, ports, etc."""
  skip_attrs = ['flattened', 'flattened_addr', 'flattened_saddr',
                'flattened_daddr', 'action', 'comment', 'name']
  if 'deny' not in term.action:
    return False
  # This lc will look through all methods and attributes of the object.
  # It returns only the attributes that need to be looked at to determine if
  # this is a default deny.
  for i in [a for a in dir(term) if not a.startswith('__') and
            a.islower() and not callable(getattr(term, a))]:
    if i in skip_attrs:
      continue
    v = getattr(term, i)
    if isinstance(v, str) and v:
      return False
    if isinstance(v, list) and v:
      return False

  return True


def IsProjectIDValid(project):
  """Return true if a project ID is valid.

  https://cloud.google.com/resource-manager/reference/rest/v1/projects

  "It must be 6 to 30 lowercase letters, digits, or hyphens. It must start with
  a letter. Trailing hyphens are prohibited."

  Args:
    project: A string.

  Returns:
    bool: True if a project ID matches the pattern and length requirements.
  """
  if len(project) < 6 or len(project) > 30:
    return False
  return bool(re.match('^[a-z][a-z0-9\\-]*[a-z0-9]$', project))


def IsVPCNameValid(vpc):
  """Return true if a VPC name is valid.

  https://cloud.google.com/compute/docs/reference/rest/v1/networks

  "The first character must be a lowercase letter, and all following characters
  (except for the last character) must be a dash, lowercase letter, or digit.
  The last character must be a lowercase letter or digit."

  Args:
    vpc: A string.

  Returns:
    bool: True if a VPC name matches the pattern and length requirements.
  """
  if len(vpc) < 1 or len(vpc) > 63:
    return False
  return bool(re.match('^[a-z]$|^[a-z][a-z0-9-]*[a-z0-9]$', vpc))
