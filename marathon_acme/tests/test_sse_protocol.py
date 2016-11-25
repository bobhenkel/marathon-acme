# -*- coding: utf-8 -*-
from operator import methodcaller

from testtools import ExpectedException
from testtools.assertions import assert_that
from testtools.matchers import AfterPreprocessing as After
from testtools.matchers import (
    Equals, Is, IsInstance, MatchesAll, MatchesStructure)
from testtools.twistedsupport import failed, succeeded
from treq.response import _Response  # FIXME
from treq.testing import StubTreq
from twisted.internet.defer import DeferredQueue
from twisted.internet.error import ConnectionDone
from twisted.web.http import Request
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET

from marathon_acme.sse_protocol import SseProtocol
from marathon_acme.tests.matchers import HasHeader, WithErrorTypeAndMessage


class DummyTransport(object):
    disconnecting = False

    def loseConnection(self):
        self.disconnecting = True


class TestSseProtocol(object):
    def setup_method(self):
        self.messages = []

        def append_message(event, data):
            self.messages.append((event, data))
        self.protocol = SseProtocol(append_message)

        self.transport = DummyTransport()
        self.protocol.makeConnection(self.transport)

    def test_default_event(self):
        """
        When data is received, followed by a blank line, the default event
        type, 'message', should be used.
        """
        self.protocol.dataReceived(b'data:hello\r\n\r\n')

        assert_that(self.messages, Equals([('message', 'hello')]))

    def test_multiline_data(self):
        """
        When multiple lines of data are specified in a single event, those
        lines should be received by the handler with a '\n' character
        separating them.
        """
        self.protocol.dataReceived(b'data:hello\r\ndata:world\r\n\r\n')

        assert_that(self.messages, Equals([('message', 'hello\nworld')]))

    def test_different_newlines(self):
        """
        When data is received with '\r\n', '\n', or '\r', lines should be split
        on those characters.
        """
        self.protocol.dataReceived(b'data:hello\ndata:world\r\r\n')

        assert_that(self.messages, Equals([('message', 'hello\nworld')]))

    def test_empty_data(self):
        """
        When the data field is specified in an event but no data is given, the
        handler should receive a message with empty data.
        """
        self.protocol.dataReceived(b'data:\r\n\r\n')

        assert_that(self.messages, Equals([('message', '')]))

    def test_no_data(self):
        """
        When the data field is not specified and the event is completed, the
        handler should not be called.
        """
        self.protocol.dataReceived(b'\r\n')

        assert_that(self.messages, Equals([]))

    def test_space_before_value(self):
        """
        When a field/value pair is received, and there is a space before the
        value, the leading space should be stripped.
        """
        self.protocol.dataReceived(b'data: hello\r\n\r\n')

        assert_that(self.messages, Equals([('message', 'hello')]))

    def test_space_before_value_strip_only_first_space(self):
        """
        When a field/value pair is received, and there are multiple spaces at
        the start of the value, the leading space should be stripped and the
        other spaces left intact.
        """
        self.protocol.dataReceived(b'data:%s\r\n\r\n' % (b' ' * 4,))

        assert_that(self.messages, Equals([('message', ' ' * 3)]))

    def test_custom_event(self):
        """
        If a custom event is set for an event, a the handler should be called
        with the correct event.
        """
        self.protocol.dataReceived(b'event:my_event\r\n')
        self.protocol.dataReceived(b'data:hello\r\n\r\n')

        assert_that(self.messages, Equals([('my_event', 'hello')]))

    def test_multiple_events(self):
        """
        If multiple different event types are received, the handler should
        receive the different event types and their corresponding data.
        """
        self.protocol.dataReceived(b'event:test1\r\n')
        self.protocol.dataReceived(b'data:hello\r\n\r\n')
        self.protocol.dataReceived(b'event:test2\r\n')
        self.protocol.dataReceived(b'data:world\r\n\r\n')

        assert_that(self.messages, Equals([
            ('test1', 'hello'),
            ('test2', 'world')
        ]))

    def test_id_ignored(self):
        """
        When the id field is included in an event, it should be ignored.
        """
        self.protocol.dataReceived(b'data:hello\r\n')
        self.protocol.dataReceived(b'id:123\r\n\r\n')

        assert_that(self.messages, Equals([('message', 'hello')]))

    def test_retry_ignored(self):
        """
        When the retry field is included in an event, it should be ignored.
        """
        self.protocol.dataReceived(b'data:hello\r\n')
        self.protocol.dataReceived(b'retry:123\r\n\r\n')

        assert_that(self.messages, Equals([('message', 'hello')]))

    def test_unknown_field_ignored(self):
        """
        When an unknown field is included in an event, it should be ignored.
        """
        self.protocol.dataReceived(b'data:hello\r\n')
        self.protocol.dataReceived(b'somefield:123\r\n\r\n')

        assert_that(self.messages, Equals([('message', 'hello')]))

    def test_leading_colon_ignored(self):
        """
        When a line is received starting with a ':' character, the line should
        be ignored.
        """
        self.protocol.dataReceived(b'data:hello\r\n')
        self.protocol.dataReceived(b':123abc\r\n\r\n')

        assert_that(self.messages, Equals([('message', 'hello')]))

    def test_missing_colon(self):
        """
        When a line is received that doesn't contain a ':' character, the whole
        line should be treated as the field and the value should be an empty
        string.
        """
        self.protocol.dataReceived(b'data\r\n')
        self.protocol.dataReceived(b'data:hello\r\n\r\n')

        assert_that(self.messages, Equals([('message', '\nhello')]))

    def test_trim_only_last_newline(self):
        """
        When multiline data is received, only the final newline character
        should be stripped before the data is passed to the handler.
        """
        self.protocol.dataReceived(b'data:\r')
        self.protocol.dataReceived(b'data:\n')
        self.protocol.dataReceived(b'data:\r\n\r\n')

        assert_that(self.messages, Equals([('message', '\n\n')]))

    def test_multiple_data_parts(self):
        """
        When data is received in multiple parts, the parts should be collected
        to form the lines of the event.
        """
        self.protocol.dataReceived(b'data:')
        self.protocol.dataReceived(b' hello\r\n')
        self.protocol.dataReceived(b'\r\n')

        assert_that(self.messages, Equals([('message', 'hello')]))

    def test_unicode_data(self):
        """
        When unicode data encoded as UTF-8 is received, the characters should
        be decoded correctly.
        """
        self.protocol.dataReceived(u'data:hëlló\r\n\r\n'.encode('utf-8'))

        assert_that(self.messages, Equals([('message', u'hëlló')]))

    def test_line_too_long(self):
        """
        When a line is received that is beyond the maximum allowed length,
        the transport should be in 'disconnecting' state due to a request to
        lose the connection.
        """
        self.protocol.MAX_LENGTH = 8  # Very long bytearrays slow down tests
        self.protocol.dataReceived(b'data:%s\r\n\r\n' % (
            b'x' * (self.protocol.MAX_LENGTH + 1),))

        assert_that(self.transport.disconnecting, Equals(True))

    def test_incomplete_line_too_long(self):
        """
        When a part of a line is received that is beyond the maximum allowed
        length, the transport should be in 'disconnecting' state due to a
        request to lose the connection.
        """
        self.protocol.MAX_LENGTH = 8  # Very long bytearrays slow down tests
        self.protocol.dataReceived(b'data:%s' % (
            b'x' * (self.protocol.MAX_LENGTH + 1),))

        assert_that(self.transport.disconnecting, Equals(True))

    def test_transport_disconnecting(self):
        """
        When the transport for the protocol is disconnecting, processing should
        be halted.
        """
        self.transport.disconnecting = True
        self.protocol.dataReceived(b'data:hello\r\n\r\n')

        assert_that(self.messages, Equals([]))

    def test_transport_connection_lost(self):
        """
        When the connection is lost, the finished deferred should be called.
        """
        finished = self.protocol.when_finished()

        self.protocol.connectionLost()

        assert_that(finished, succeeded(Is(None)))

    def test_transport_connection_lost_no_callback(self):
        """
        When the connection is lost and the finished deferred hasn't been set,
        nothing should happen.
        """
        self.protocol.connectionLost()

    def test_multiple_events_resets_the_event_type(self):
        """
        After an event is consumed with a custom event type, the event type
        should be reset to the default, and the handler should receive further
        messages with the default event type.
        """
        # Event 1
        self.protocol.dataReceived(b'event:status\r\n')
        self.protocol.dataReceived(b'data:hello\r\n')
        self.protocol.dataReceived(b'\r\n')

        # Event 2
        self.protocol.dataReceived(b'data:world\r\n')
        self.protocol.dataReceived(b'\r\n')

        assert_that(self.messages, Equals([
            ('status', 'hello'),
            ('message', 'world')
        ]))

    def test_transport_without_methods_connected(self):
        """
        When a connection is made with a transport object that does not have
        the methods we need to be able to close the connection, an error should
        be raised.
        """
        with ExpectedException(
            ValueError,
            r"^Transport '<object.*>' does not have a 'loseConnection' or "
                "'stopProducing' method"):
            self.protocol.makeConnection(object())


class FakeSseResource(Resource):
    """
    A leaf resource that just writes SSE headers to incoming requests and
    stores them to a queue.
    """
    isLeaf = True

    def __init__(self):
        self.requests = DeferredQueue()

    def render(self, request):
        request.setResponseCode(200)
        request.setHeader('Content-Type', 'text/event-stream')
        request.write(b'')  # Write empty bytes to flush headers
        self.requests.put(request)
        return NOT_DONE_YET


def is_sse_request():
    return MatchesAll(
        IsInstance(Request),
        MatchesStructure(method=Equals(b'GET')),
        After(
            methodcaller('getHeader', b'accept'), Equals(b'text/event-stream'))
    )


def is_sse_response():
    return MatchesAll(
        IsInstance(_Response),
        MatchesStructure(
            code=Equals(200),
            headers=HasHeader('content-type', ['text/event-stream'])
        )
    )


class TestSseProtocolIntegration(object):
    """
    These are only integration tests in the sense that they test the way the
    protocol interacts with HTTP transport machinery that it is likely to be
    used with.
    """

    def setup_method(self):
        self.messages = []

        def append_message(event, data):
            self.messages.append((event, data))
        self.protocol = SseProtocol(append_message)

        resource = FakeSseResource()
        self.requests = resource.requests
        self.client = StubTreq(resource)

    def make_request(self):
        """
        Make an SSE request.
        :return: The Twisted request object and the Treq response object
        """
        request_d = self.requests.get()

        def deliver_body(response):
            response.deliverBody(self.protocol)
            return response

        response_d = self.client.get(
            'http://localhost', headers={'accept': 'text/event-stream'})
        response_d.addCallback(deliver_body)

        # The HTTP parts of the protocol (headers, etc.) we don't really care
        # about at the transport-level but we sanity-check that the deferreds
        # have what we expect.
        assert_that(request_d, succeeded(is_sse_request()))
        assert_that(response_d, succeeded(is_sse_response()))

        return request_d.result, response_d.result

    def write(self, request, data):
        """ Write data to a Request object. """
        request.write(data)
        self.client.flush()

    def lose_connection(self, request):
        """ Lose the connection on a Request object. """
        request.loseConnection()
        self.client.flush()

    def test_write(self):
        """
        When SSE data is written to the request, messages should be interpreted
        by the protocol.
        """
        request, response = self.make_request()

        self.write(request, b'data:hello\r\n\r\n')
        assert_that(self.messages, Equals([('message', 'hello')]))

    def test_lose_connection(self):
        """
        When the request connection is lost, a finished deferred on the
        protocol should be fired with a None value.
        """
        request, response = self.make_request()

        finished = self.protocol.when_finished()

        self.lose_connection(request)

        assert_that(finished, succeeded(Is(None)))

    def test_cancel_finished_deferred(self):
        """
        When a finished deferred is cancelled, the underlying connection should
        be lost and any other finished deferreds should be fired.
        """
        # Make a request so that a transport is attached
        request, _ = self.make_request()
        request_finished = request.notifyFinish()

        # Get two finished deferreds...
        finished1 = self.protocol.when_finished()
        finished2 = self.protocol.when_finished()

        # ...and cancel one of them
        finished1.cancel()
        self.client.flush()

        assert_that(request_finished, failed(WithErrorTypeAndMessage(
            ConnectionDone, 'Connection was closed cleanly: Connection done.'
        )))
        assert_that(finished2, succeeded(Is(None)))
