from __future__ import absolute_import
import ssl
import httplib
import socket
import errno

from . import helpers
from . import itarget_connection
from . import ip_constants
from . import sex

ETH_P_IP = 0x0800  # Ethernet protocol: Internet Protocol packet, see Linux if_ether.h docs for more details.


class SocketConnection(itarget_connection.ITargetConnection):
    """ITargetConnection implementation using sockets.

    Supports UDP, TCP, SSL, raw layer 2 and raw layer 3 packets.

    Examples:
        tcp_connection = SocketConnection(host='127.0.0.1', port=17971)
        udp_connection = SocketConnection(host='127.0.0.1', port=17971, proto='udp')
    """
    _PROTOCOLS = ["tcp", "ssl", "udp", "raw-l2", "raw-l3"]
    _PROTOCOLS_PORT_REQUIRED = ["tcp", "ssl", "udp"]
    MAX_PAYLOADS = {"raw-l2": 1514,
                    "raw-l3": 1500,
                    # UDPv4 theoretical limit; actual value dynamically generated by constructor:
                    "udp": ip_constants.UDP_MAX_LENGTH,
                    }

    def __init__(self,
                 host,
                 port=None,
                 proto="tcp",
                 bind=None,
                 timeout=5.0,
                 ethernet_proto=ETH_P_IP,
                 l2_dst='\xFF' * 6):
        """
        @type  host:    str
        @param host:    Hostname or IP address of target system,
                        or network interface string if using raw-l2 or raw-l3.

        @type  port:    int
        @param port:    Port of target service. Required for proto values 'tcp', 'udp', 'ssl'.

        @type  proto:   str
        @kwarg proto:   (Optional, def="tcp") Communication protocol ("tcp", "udp", "ssl", "raw-l2", "raw-l3")
                        raw-l2: Send packets at layer 2. Must include link layer header (e.g. Ethernet frame).
                        raw-l3: Send packets at layer 3. Must include network protocol header (e.g. IPv4).

        @type  bind:    tuple (host, port)
        @kwarg bind:    (Optional, def=None) Socket bind address and port. Required if using recv() with 'udp' protocol.

        @type  timeout: float
        @kwarg timeout: (Optional, def=5.0) Seconds to wait for a send/recv prior to timing out

        @type ethernet_proto:
                        int
        @kwarg ethernet_proto:
                        (Optional, def=ETH_P_IP (0x0800)) Ethernet protocol when using 'raw-l3'. 16 bit integer.
                        See "if_ether.h" in Linux documentation for more options.

        @type l2_dst:   str
        @kwarg l2_dst:  (Optional, def='\xFF\xFF\xFF\xFF\xFF\xFF' (broadcast))
                        Layer 2 destination address (e.g. MAC address). Used only by 'raw-l3'.
        """
        self.MAX_PAYLOADS["udp"] = helpers.get_max_udp_size()

        self.host = host
        self.port = port
        self.bind = bind
        self.timeout = timeout
        self.proto = proto.lower()
        self.ethernet_proto = ethernet_proto
        self.l2_dst = l2_dst

        self._sock = None

        if self.proto not in self._PROTOCOLS:
            raise sex.SullyRuntimeError("INVALID PROTOCOL SPECIFIED: %s" % self.proto)

        if self.proto in self._PROTOCOLS_PORT_REQUIRED and self.port is None:
            raise ValueError("__init__() argument port required for protocol {0}".format(self.proto))

    def close(self):
        """
        Close connection to the target.

        :return: None
        """
        self._sock.close()

    def open(self):
        """
        Opens connection to the target. Make sure to call close!

        :return: None
        """
        # Create socket
        if self.proto == "tcp" or self.proto == "ssl":
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        elif self.proto == "udp":
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if self.bind:
                self._sock.bind(self.bind)
        elif self.proto == "raw-l2":
            self._sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        elif self.proto == "raw-l3":
            self._sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM)
        else:
            raise sex.SullyRuntimeError("INVALID PROTOCOL SPECIFIED: %s" % self.proto)

        self._sock.settimeout(self.timeout)

        # Connect is needed only for TCP protocols
        if self.proto == "tcp" or self.proto == "ssl":
            try:
                self._sock.connect((self.host, self.port))
            except socket.error as e:
                if e.errno == errno.ECONNREFUSED:
                    raise sex.BoofuzzTargetConnectionFailedError(e.message)
                else:
                    raise

        # if SSL is requested, then enable it.
        if self.proto == "ssl":
            ssl_sock = ssl.wrap_socket(self._sock)
            self._sock = httplib.FakeSocket(self._sock, ssl_sock)

    def recv(self, max_bytes):
        """
        Receive up to max_bytes data from the target.

        :param max_bytes: Maximum number of bytes to receive.
        :type max_bytes: int

        :return: Received data.
        """
        try:
            if self.proto in ['tcp', 'ssl']:
                data = self._sock.recv(max_bytes)
            elif self.proto == 'udp':
                if self.bind:
                    data, _ = self._sock.recvfrom(max_bytes)
                else:
                    raise sex.SullyRuntimeError(
                        "SocketConnection.recv() for UDP requires a bind address/port."
                        " Current value:".format(self.bind))
            elif self.proto in ['raw-l2', 'raw-l3']:
                # receive on raw is not supported. Since there is no specific protocol for raw, we would just have to
                # dump everything off the interface anyway, which is probably not what the user wants.
                data = bytes('')
            else:
                raise sex.SullyRuntimeError("INVALID PROTOCOL SPECIFIED: %s" % self.proto)
        except socket.timeout:
            data = bytes('')
        except socket.error as e:
            if (e.errno == errno.ECONNABORTED) or \
               (e.errno == errno.ECONNREFUSED) or \
               (e.errno == errno.ECONNRESET) or  \
               (e.errno == errno.ENETRESET) or \
               (e.errno == errno.ETIMEDOUT):
                data = bytes('')
            else:
                raise

        return data

    def send(self, data):
        """
        Send data to the target. Only valid after calling open!
        Some protocols will truncate; see self.MAX_PAYLOADS.

        :param data: Data to send.

        :rtype int
        :return: Number of bytes actually sent.
        """
        try:
            data = data[:self.MAX_PAYLOADS[self.proto]]
        except KeyError:
            pass  # data = data

        if self.proto in ["tcp", "ssl"]:
            num_sent = self._sock.send(data)
        elif self.proto == "udp":
            num_sent = self._sock.sendto(data, (self.host, self.port))
        elif self.proto == "raw-l2":
            num_sent = self._sock.sendto(data, (self.host, 0))
        elif self.proto == "raw-l3":
            # Address tuple: (interface string,
            #                 Ethernet protocol number,
            #                 packet type (recv only),
            #                 hatype (recv only),
            #                 Ethernet address)
            # See man 7 packet for more details.
            num_sent = self._sock.sendto(data, (self.host, self.ethernet_proto, 0, 0, self.l2_dst))
        else:
            raise sex.SullyRuntimeError("INVALID PROTOCOL SPECIFIED: %s" % self.proto)

        return num_sent
