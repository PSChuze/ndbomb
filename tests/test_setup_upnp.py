"""Router port forwarding (hostsetup/upnp.py) against a fake gateway."""
import unittest

from hostsetup import upnp


class FakeGateway:
    def __init__(self, taken=()):
        self.maps = {}
        self.taken = set(taken)

    def mapping(self, port, protocol="TCP"):
        return self.maps.get((port, protocol))

    def add(self, port, client, description, protocol="TCP", lease=0):
        if (port, protocol) in self.taken:
            raise upnp.UpnpError("conflict", upnp.CONFLICT)
        self.maps[(port, protocol)] = (client, port)


class UpnpTest(unittest.TestCase):
    def test_tcp_and_udp_are_forwarded_separately(self):
        gw = FakeGateway()
        out = upnp.open_ports(gw, [(10020, "TCP"), (10020, "UDP")], "192.168.0.10")
        self.assertEqual(out, {(10020, "TCP"): "opened", (10020, "UDP"): "opened"})
        self.assertEqual(set(gw.maps), {(10020, "TCP"), (10020, "UDP")})

    def test_already_and_taken(self):
        gw = FakeGateway(taken=[(80, "TCP")])
        gw.maps[(9901, "TCP")] = ("192.168.0.10", 9901)
        out = upnp.open_ports(gw, [(80, "TCP"), (9901, "TCP")], "192.168.0.10")
        self.assertEqual(out, {(80, "TCP"): "taken", (9901, "TCP"): "already"})

    def test_private_addresses(self):
        for ip in ("192.168.0.1", "10.0.0.1", "172.16.0.1", "100.64.0.1"):
            self.assertTrue(upnp.is_private(ip), ip)
        self.assertFalse(upnp.is_private("203.0.113.5"))


if __name__ == "__main__":
    unittest.main()
