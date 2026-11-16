"""Minimal re-implementation of the subset of ansible's `ipaddr` filter used by
the Fortinet Jinja Orchestrator templates (no ansible/netaddr dependency)."""
import ipaddress


def _iface(value):
    try:
        return ipaddress.ip_interface(str(value).strip())
    except ValueError:
        return None


def ipaddr(value, query=None):
    i = _iface(value)
    if i is None:
        return False
    if query is None or query == "":
        return str(i) if "/" in str(value) else str(i.ip)
    if isinstance(query, bool):
        return False
    if isinstance(query, int):          # nth address of the network, keeps prefix
        net = i.network
        base = int(net.network_address) if query >= 0 else int(net.broadcast_address) + 1
        try:
            return f"{ipaddress.ip_address(base + query)}/{net.prefixlen}"
        except ValueError:
            return False
    if query == "address":
        return str(i.ip)
    if query == "netmask":
        return str(i.netmask)
    if query == "prefix":
        return str(i.network.prefixlen)
    if query == "network":
        return str(i.network.network_address)
    raise ValueError(f"ipaddr query not supported: {query!r}")
