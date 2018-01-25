from future import standard_library
standard_library.install_aliases()
from email.mime.text import MIMEText
import glob
from io import StringIO
import os
from pkg_resources import resource_stream
import sys
import urllib.parse

from OpenSSL import SSL
from OpenSSL.crypto import load_certificate, FILETYPE_PEM

from twisted.internet import reactor, ssl
from twisted.internet.error import DNSLookupError, TimeoutError
from twisted.internet.abstract import isIPAddress
from twisted.internet.defer import (
    Deferred,
    inlineCallbacks,
    )
from twisted.internet.protocol import (
    ClientCreator,
    DatagramProtocol,
    Protocol,
    )
from twisted.mail.smtp import ESMTPSenderFactory
from twisted.protocols.memcache import MemCacheProtocol

from txrequests import Session
from requests.auth import HTTPDigestAuth

try:
    from requests.packages.urllib3 import disable_warnings
    from requests.packages.urllib3.contrib.pyopenssl import inject_into_urllib3
except ImportError:
    from urllib3 import disable_warnings
    from urllib3.contrib.pyopenssl import inject_into_urllib3

from .check_impl import (
    add_check_prefix,
    make_check,
    sequential_check,
    )


# Ensure we always use pyOpenSSL instead of the ssl builtin
inject_into_urllib3()

CA_CERTS = []


def load_tls_certs(path):
    cert_map = {}
    for filepath in glob.glob("{}/*.pem".format(os.path.abspath(path))):
        # There might be some dead symlinks in there,
        # so let's make sure it's real.
        if os.path.isfile(filepath):
            data = open(filepath).read()
            x509 = load_certificate(FILETYPE_PEM, data)
            # Now, de-duplicate in case the same cert has multiple names.
            cert_map[x509.digest('sha1')] = x509

    CA_CERTS.extend(list(cert_map.values()))


class TCPCheckProtocol(Protocol):

    def connectionMade(self):
        self.transport.loseConnection()


class VerifyingContextFactory(ssl.CertificateOptions):

    def __init__(self, verify, caCerts, verifyCallback=None):
        ssl.CertificateOptions.__init__(self, verify=verify,
                                        caCerts=caCerts,
                                        method=SSL.SSLv23_METHOD)
        self.verifyCallback = verifyCallback

    def _makeContext(self):
        context = ssl.CertificateOptions._makeContext(self)
        if self.verifyCallback is not None:
            context.set_verify(
                SSL.VERIFY_PEER | SSL.VERIFY_FAIL_IF_NO_PEER_CERT,
                self.verifyCallback)
        return context


@inlineCallbacks
def do_tcp_check(host, port, tls=False, tls_verify=True,
                 timeout=None):
    """Generic connection check function."""
    if not isIPAddress(host):
        try:
            ip = yield reactor.resolve(host, timeout=(1, timeout))
        except DNSLookupError:
            raise ValueError("dns resolution failed")
    else:
        ip = host
    creator = ClientCreator(reactor, TCPCheckProtocol)
    try:
        if tls:
            context = VerifyingContextFactory(tls_verify, CA_CERTS)
            yield creator.connectSSL(ip, port, context,
                                     timeout=timeout)
        else:
            yield creator.connectTCP(ip, port, timeout=timeout)
    except TimeoutError:
        if ip == host:
            raise ValueError("timed out")
        else:
            raise ValueError("timed out connecting to {}".format(ip))


def make_tcp_check(host, port, timeout=None, **kwargs):
    """Return a check for TCP connectivity."""
    return make_check("tcp:{}:{}".format(host, port),
                      lambda: do_tcp_check(host, port, timeout=timeout),
                      info="{}:{}".format(host, port))


def make_tls_check(host, port, disable_tls_verification=False, timeout=None,
                   **kwargs):
    """Return a check for TLS setup."""

    verify = not disable_tls_verification
    check = make_check("tls:{}:{}".format(host, port),
                       lambda: do_tcp_check(host, port, tls=True,
                                            tls_verify=verify,
                                            timeout=timeout),
                       info="{}:{}".format(host, port))

    return check


class UDPCheckProtocol(DatagramProtocol):

    def __init__(self, host, port, send, expect, deferred=None,
                 timeout=None):
        self.host = host
        self.port = port
        self.send = send
        self.expect = expect
        self.deferred = deferred
        self.timeout = timeout

    def _finish(self, success, result):
        if not (self.delayed.cancelled or self.delayed.called):
            self.delayed.cancel()
        if self.deferred is not None:
            if success:
                self.deferred.callback(result)
            else:
                self.deferred.errback(result)
            self.deferred = None

    def startProtocol(self):
        self.transport.write(self.send, (self.host, self.port))
        self.delayed = reactor.callLater(self.timeout,
                                         self._finish,
                                         False, TimeoutError())

    def datagramReceived(self, datagram, addr):
        if datagram == self.expect:
            self._finish(True, True)
        else:
            self._finish(False, ValueError("unexpected reply"))


@inlineCallbacks
def do_udp_check(host, port, send, expect, timeout=None):
    """Generic connection check function."""
    if not isIPAddress(host):
        try:
            ip = yield reactor.resolve(host, timeout=(1, timeout))
        except DNSLookupError:
            raise ValueError("dns resolution failed")
    else:
        ip = host
    deferred = Deferred()
    protocol = UDPCheckProtocol(ip, port, send, expect, deferred, timeout)
    reactor.listenUDP(0, protocol)
    try:
        yield deferred
    except TimeoutError:
        if ip == host:
            raise ValueError("timed out")
        else:
            raise ValueError("timed out waiting for {}".format(ip))


def make_udp_check(host, port, send, expect, timeout=None,
                   **kwargs):
    """Return a check for UDP connectivity."""
    return make_check("udp:{}:{}".format(host, port),
                      lambda: do_udp_check(host, port, send, expect, timeout),
                      info="{}:{}".format(host, port))


def extract_host_port(url):
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port
    scheme = parsed.scheme
    if not scheme:
        scheme = 'http'
    if port is None:
        if scheme == 'https':
            port = 443
        else:
            port = 80
    return host, port, scheme


def make_http_check(url, method='GET', expected_code=200, **kwargs):
    subchecks = []
    host, port, scheme = extract_host_port(url)
    proxy_url = kwargs.get('proxy_url')
    proxy_host = kwargs.get('proxy_host')
    proxy_port = int(kwargs.get('proxy_port', 8000))
    timeout = kwargs.get('timeout', None)
    expected_code = int(expected_code)

    if proxy_host:
        subchecks.append(make_tcp_check(proxy_host, proxy_port,
                                        timeout=timeout))
    else:
        subchecks.append(make_tcp_check(host, port, timeout=timeout))

    @inlineCallbacks
    def do_request():
        proxies = {}
        if proxy_url:
            proxies['http'] = proxies['https'] = proxy_url
        elif proxy_host:
            proxies['http'] = proxies['https'] = '{}:{}'.format(
                proxy_host, proxy_port)

        headers = kwargs.get('headers')
        body = kwargs.get('body')
        disable_tls_verification = kwargs.get('disable_tls_verification',
                                              False)
        allow_redirects = kwargs.get('allow_redirects', False)
        params = kwargs.get('params')
        cookies = kwargs.get('cookies')
        auth = kwargs.get('auth')
        digest_auth = kwargs.get('digest_auth')

        args = {
            'method': method,
            'url': url,
            'verify': not disable_tls_verification,
            'timeout': timeout,
            'allow_redirects': allow_redirects,
        }
        if headers:
            args['headers'] = headers
        if body:
            args['data'] = body
        if proxies:
            args['proxies'] = proxies
        if params:
            args['params'] = params
        if cookies:
            args['cookies'] = cookies
        if auth:
            args['auth'] = auth
        if digest_auth:
            args['auth'] = HTTPDigestAuth(digest_auth)

        if disable_tls_verification:
            disable_warnings()

        with Session() as session:
            request = session.request(**args)

            response = yield request
            if response.status_code != expected_code:
                raise RuntimeError(
                    "Unexpected response code: {}".format(
                        response.status_code))

    subchecks.append(make_check('', do_request,
                     info='{} {}'.format(method, url)))

    return add_check_prefix('http:{}'.format(url),
                            sequential_check(subchecks))


def make_amqp_check(host, port, username, password, use_tls=True, vhost="/",
                    timeout=None, **kwargs):
    """Return a check for AMQP connectivity."""
    from txamqp.protocol import AMQClient
    from txamqp.client import TwistedDelegate
    from txamqp.spec import load as load_spec

    subchecks = []
    subchecks.append(make_tcp_check(host, port, timeout=timeout))

    if use_tls:
        subchecks.append(make_tls_check(host, port, verify=False,
                                        timeout=timeout))

    @inlineCallbacks
    def do_auth():
        """Connect and authenticate."""
        delegate = TwistedDelegate()
        spec = load_spec(resource_stream('conn_check', 'amqp0-8.xml'))
        creator = ClientCreator(reactor, AMQClient,
                                delegate, vhost, spec)
        client = yield creator.connectTCP(host, port, timeout=timeout)
        yield client.authenticate(username, password)

    subchecks.append(make_check("amqp:{}:{}".format(host, port),
                                do_auth, info="user {}".format(username),))
    return sequential_check(subchecks)


def make_smtp_check(host, port, username, password, from_address, to_address,
                    message='', subject='', helo_fallback=False, use_tls=True,
                    timeout=None, **kwargs):
    """Return a check for SMTP connectivity."""

    subchecks = []
    subchecks.append(make_tcp_check(host, port, timeout=timeout))

    if use_tls:
        subchecks.append(make_tls_check(host, port, verify=False,
                                        timeout=timeout))

    @inlineCallbacks
    def do_connect():
        """Connect and authenticate."""
        result_deferred = Deferred()
        context_factory = None
        if use_tls:
            from twisted.internet import ssl as twisted_ssl
            context_factory = twisted_ssl.ClientContextFactory()

        body = MIMEText(message)
        body['Subject'] = subject
        factory = ESMTPSenderFactory(
            username,
            password,
            from_address,
            to_address,
            StringIO(body.as_string()),
            result_deferred,
            contextFactory=context_factory,
            requireTransportSecurity=use_tls,
            requireAuthentication=True,
            heloFallback=helo_fallback)

        if use_tls:
            reactor.connectSSL(host, port, factory, context_factory)
        else:
            reactor.connectTCP(host, port, factory)
        result = yield result_deferred

        if result[0] == 0:
            raise RuntimeError("failed to send email via smtp")

    subchecks.append(make_check("smtp:{}:{}".format(host, port),
                                do_connect, info="user {}".format(username),))
    return sequential_check(subchecks)


def make_postgres_check(host, port, username, password, database,
                        timeout=None, **kwargs):
    """Return a check for Postgres connectivity."""

    import psycopg2
    subchecks = []
    connect_kw = {
        'host': host,
        'user': username,
        'database': database,
        'connect_timeout': timeout,
    }

    if host[0] != '/':
        connect_kw['port'] = port
        subchecks.append(make_tcp_check(host, port, timeout=timeout))

    if password is not None:
        connect_kw['password'] = password

    def check_auth():
        """Try to establish a postgres connection and log in."""
        conn = psycopg2.connect(**connect_kw)
        conn.close()

    subchecks.append(make_check("postgres:{}:{}".format(host, port),
                                check_auth, info="user {}".format(username),
                                blocking=True))

    return sequential_check(subchecks)


def make_redis_check(host, port, password=None, timeout=None,
                     **kwargs):
    """Make a check for the configured redis server."""
    if sys.version_info[0] >=3:
        raise EnvironmentError('Redis checks are not supported in python 3')
    import txredis
    subchecks = []
    subchecks.append(make_tcp_check(host, port, timeout=timeout))

    @inlineCallbacks
    def do_connect():
        """Connect and authenticate.
        """
        client_creator = ClientCreator(reactor, txredis.client.RedisClient)
        client = yield client_creator.connectTCP(host=host, port=port,
                                                 timeout=timeout)

        if password is None:
            ping = yield client.ping()
            if not ping:
                raise RuntimeError("failed to ping redis")
        else:
            resp = yield client.auth(password)
            if resp != 'OK':
                raise RuntimeError("failed to auth to redis")

    if password is not None:
        connect_info = "connect with auth"
    else:
        connect_info = "connect"
    subchecks.append(make_check(connect_info, do_connect))

    return add_check_prefix('redis:{}:{}'.format(host, port),
                            sequential_check(subchecks))


def make_memcache_check(host, port, password=None, timeout=None,
                        **kwargs):
    """Make a check for the configured redis server."""
    subchecks = []
    subchecks.append(make_tcp_check(host, port, timeout=timeout))

    @inlineCallbacks
    def do_connect():
        """Connect and authenticate."""
        client_creator = ClientCreator(reactor, MemCacheProtocol)
        client = yield client_creator.connectTCP(host=host, port=port,
                                                 timeout=timeout)

        version = yield client.version()
        if version is None:
            raise RuntimeError('Failed retrieve memcached server version')

    subchecks.append(make_check('connect', do_connect))

    return add_check_prefix('memcache:{}:{}'.format(host, port),
                            sequential_check(subchecks))


def make_mongodb_check(host, port=27017, username=None, password=None,
                       database='test', timeout=10, **kwargs):
    """Return a check for MongoDB connectivity."""

    import txmongo
    subchecks = []
    subchecks.append(make_tcp_check(host, port, timeout=timeout))

    port = int(port)

    @inlineCallbacks
    def do_connect():
        """Try to establish a mongodb connection."""
        conn = txmongo.MongoConnection(host, port)

        conn.uri['options']['connectTimeoutMS'] = int(timeout*1000)
        if username:
            conn.uri['username'] = username
        if password:
            conn.uri['password'] = password

        # We don't start our timeout callback until now, otherwise we might
        # elapse part of our timeout period during the earlier TCP check
        reactor.callLater(timeout, timeout_handler)

        mongo = yield conn
        names = yield mongo[database].collection_names()
        if names is None:
            raise RuntimeError('Failed to retrieve collection names')

    def timeout_handler():
        """Manual timeout handler as txmongo timeout args don't always work."""
        if 'deferred' in do_connect.__dict__:
            err = ValueError("timeout connecting to mongodb")
            do_connect.__dict__['deferred'].errback(err)

    if any((username, password)):
        connect_info = "connect with auth"
    else:
        connect_info = "connect"
    subchecks.append(make_check(connect_info, do_connect))

    return add_check_prefix('mongodb:{}:{}'.format(host, port),
                            sequential_check(subchecks))


CHECKS = {
    'tcp': {
        'fn': make_tcp_check,
        'args': ['host', 'port'],
    },
    'tls': {
        'fn': make_tls_check,
        'args': ['host', 'port'],
    },
    'udp': {
        'fn': make_udp_check,
        'args': ['host', 'port', 'send', 'expect'],
    },
    'http': {
        'fn': make_http_check,
        'args': ['url'],
    },
    'amqp': {
        'fn': make_amqp_check,
        'args': ['host', 'port', 'username', 'password'],
    },
    'postgres': {
        'fn': make_postgres_check,
        'args': ['host', 'port', 'username', 'password', 'database'],
    },
    'redis': {
        'fn': make_redis_check,
        'args': ['host', 'port'],
    },
    'memcache': {
        'fn': make_memcache_check,
        'args': ['host', 'port'],
    },
    'mongodb': {
        'fn': make_mongodb_check,
        'args': ['host'],
    },
    'smtp': {
        'fn': make_smtp_check,
        'args': ['host', 'port', 'username', 'password'],
    },
}

CHECK_ALIASES = {
    'mongo': 'mongodb',
    'memcached': 'memcache',
    'ssl': 'tls',
    'postgresql': 'postgres',
    'https': 'http',
}
