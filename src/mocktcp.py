import asyncio
import logging
from dataclasses import dataclass

import pytest


# logger = logging.getLogger(__name__)


@dataclass
class ServerActionEvent:
    pass


@dataclass
class ClientConnectedEvent(ServerActionEvent):
    pass


@dataclass
class SecondClientConnectionAttempted(ServerActionEvent):
    pass


@dataclass
class ReadZeroBytes(ServerActionEvent):
    pass


@dataclass
class ClientCalledWriterWaitClosed(ServerActionEvent):
    pass


@dataclass
class NoRemainingSentData(ServerActionEvent):
    pass


@dataclass
class ExceptionEvent(ServerActionEvent):

    exception: Exception


@dataclass
class BytesReadEvent(ServerActionEvent):

    bytes_read: bytes


@dataclass
class TimeoutEvent(ServerActionEvent):
    pass


@dataclass
class UnreadSentBytes(ServerActionEvent):

    def __init__(self, unread_bytes):
        self.unread_bytes = unread_bytes


class UnexpectedEventError(Exception):

    def __init__(self, expected_event, actual_event):
        super().__init__(f"UnexpectedEventError(expected_event={expected_event}, actual_event={actual_event}")
        self.expected_event = expected_event
        self.actual_event = actual_event


class ExpectConnect:

    def __init__(self, server, timeout):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.server = server
        self.timeout = timeout

    def __str__(self):
        return "ExpectConnect()"

    async def server_action(self):
        # See `MockTcpServer.start` for why this method cannot itself generate
        # the `ClientConnectedEvent`
        pass

    async def evaluate(self):
        # Since `server_action` does nothing, it cannot generate an error event in the
        # case of a timeout. We have to do that here.

        try:
            next_event = await asyncio.wait_for(
                self.server.server_event_queue.get(),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            next_event = TimeoutEvent()

        self.logger.debug("next_event: %s", next_event)
        if not isinstance(next_event, ClientConnectedEvent):
            raise UnexpectedEventError(ClientConnectedEvent(), next_event)


class ExpectClientCalledWriterWaitClosed:

    def __init__(self, server, timeout):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.server = server
        self.timeout = timeout

    async def server_action(self):
        # `client_writer_close` does this
        pass

    async def evaluate(self):
        # Since `server_action` does nothing, it cannot generate an error event in the
        # case of a timeout. We have to do that here.

        try:
            await asyncio.wait_for(
                self.server.client_called_writer_waited_closed.wait(),
                timeout=self.timeout,
            )
            next_event = ClientCalledWriterWaitClosed()
        except asyncio.TimeoutError:
            next_event = TimeoutEvent()

        self.logger.debug("next_event: %s", next_event)
        if not isinstance(next_event, ClientCalledWriterWaitClosed):
            raise UnexpectedEventError(ClientCalledWriterWaitClosed(), next_event)


class ExpectBytes:

    def __init__(self, server, expected_bytes, timeout):
        self.server = server
        self.expected_bytes = expected_bytes
        self.timeout = timeout
        self.logger = logging.getLogger(str(self))

    def __str__(self):
        return f"ExpectBytes(expected_bytes={self.expected_bytes})"

    async def server_action(self):
        try:
            received = await asyncio.wait_for(
                self.server.reader.readexactly(len(self.expected_bytes)),
                timeout=self.timeout,
            )
            return BytesReadEvent(received)
        except asyncio.TimeoutError as e:
            return TimeoutEvent()

    async def evaluate(self):
        self.logger.debug("Retrieving server event...")
        next_event = await self.server.server_event_queue.get()
        self.logger.debug("next_event: %s", next_event)
        if not isinstance(next_event, BytesReadEvent):
            raise UnexpectedEventError(BytesReadEvent(self.expected_bytes), next_event)
        if next_event.bytes_read != self.expected_bytes:
            raise UnexpectedEventError(BytesReadEvent(self.expected_bytes), next_event)


class ExpectReadZeroBytes:

    def __init__(self, server, timeout):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.server = server
        self.timeout = timeout

    def __str__(self):
        return f"ExpectReadZeroBytes(timeout={self.timeout})"

    async def server_action(self):
        try:
            received = await asyncio.wait_for(
                self.server.reader.read(),
                timeout=self.timeout,
            )
            self.logger.debug("received=%s", received)
            if len(received) == 0:
                return ReadZeroBytes()
            else:
                return BytesReadEvent(received)
        except asyncio.TimeoutError:
            return TimeoutEvent()
        except ConnectionResetError as e:
            return ReadZeroBytes()

    async def evaluate(self):
        self.logger.debug("enter")
        next_event = await self.server.server_event_queue.get()
        self.logger.debug("next_event: %s", next_event)
        if not isinstance(next_event, ReadZeroBytes):
            raise UnexpectedEventError(ReadZeroBytes(), next_event)


class ExpectClientReadAllSentBytes:

    def __init__(self, server, timeout):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.server = server
        self.timeout = timeout

    async def server_action(self):
        self.logger.debug("enter")
        try:
            sent_bytes = self.server.data_sent_from_server
            read_bytes = self.server.data_read_by_client
            if read_bytes == sent_bytes:
                return NoRemainingSentData()
            else:
                assert sent_bytes.startswith(read_bytes)
                return UnreadSentBytes(sent_bytes[len(read_bytes):])
        except asyncio.TimeoutError:
            return TimeoutEvent()

    async def evaluate(self):
        self.logger.debug("enter")
        next_event = await self.server.server_event_queue.get()
        self.logger.debug("next_event: %s", next_event)
        if not isinstance(next_event, NoRemainingSentData):
            raise UnexpectedEventError(NoRemainingSentData(), next_event)


class SendBytes:

    def __init__(self, server, data):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.server = server
        self.data = data

    def __str__(self):
        return f"SendBytes(data={self.data})"

    async def server_action(self):
        try:
            self.logger.debug("About to send bytes %s", self.data)
            self.server.writer.write(self.data)
            self.server.data_sent_from_server += self.data
            self.logger.debug("Bytes written")
        except BaseException:
            self.logger.exception("SendBytes")
        # await self.server.writer.drain()

    async def evaluate(self):
        pass


def interpret_error(exception):

    if isinstance(exception, UnexpectedEventError):
        expected_event = exception.expected_event
        actual_event = exception.actual_event
        if isinstance(expected_event, ReadZeroBytes):
            if isinstance(actual_event, SecondClientConnectionAttempted):
                return "While waiting for client to disconnect a second connection was attempted"
            elif isinstance(actual_event, TimeoutEvent):
                return "Timed out waiting for client to disconnect. Remember to call `writer.close()`."
            elif isinstance(actual_event, BytesReadEvent):
                return f"Received unexpected data while waiting for client to disconnect. Data is {actual_event.bytes_read}."
        elif isinstance(expected_event, ClientConnectedEvent):
            if isinstance(actual_event, TimeoutEvent):
                return "Timed out waiting for client to connect"
        elif isinstance(expected_event, BytesReadEvent):
            if isinstance(actual_event, TimeoutEvent):
                return f"Timed out waiting for {expected_event.bytes_read}"
            elif isinstance(actual_event, ClientConnectedEvent):
                return f"Missing `expect_connect()` before `expect_bytes({expected_event.bytes_read})`"
            elif isinstance(actual_event, BytesReadEvent):
                return f"Expected to read {expected_event.bytes_read} but actually read {actual_event.bytes_read}"
        elif isinstance(expected_event, ClientCalledWriterWaitClosed):
            if isinstance(actual_event, TimeoutEvent):
                return "Timed out waiting for client to call `await writer.wait_closed()`."
        elif isinstance(expected_event, NoRemainingSentData):
            if isinstance(actual_event, UnreadSentBytes):
                return f"There is data sent by server that was not read by client: unread_bytes={actual_event.unread_bytes}."
    return f"Cannot interpret {exception}"


async def read_unread_client_bytes(client_reader):
    # Read any remaining bytes that have been left unread
    try:
        remaining_bytes = b""
        while not client_reader.at_eof():
            # Read one byte at a time until there are none left. If the
            # stream has not been closed, we'll time out once there are no
            # bytes left
            remaining_bytes += await asyncio.wait_for(
                client_reader.read(1),
                timeout=0.1
            )
    except asyncio.TimeoutError:
        pass
    return remaining_bytes



class InterceptorProtocol:

    def __init__(self, server, original_protocol):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.server = server
        self.original_protocol = original_protocol

    def connection_made(self, transport):
        self.logger.debug("enter")
        self.original_protocol.connection_made(transport)

    def connection_lost(self, exc):
        self.logger.debug("enter: exc=%s", exc)
        self.original_protocol.connection_lost(exc)

    def pause_writing(self):
        self.original_protocol.pause_writing()

    def resume_writing(self):
        self.original_protocol.resume_writing()

    def data_received(self, data):
        self.logger.debug("begin: data=%s", data)
        self.original_protocol.data_received(data)

    def eof_received(self):
        self.logger.debug("begin")
        self.original_protocol.eof_received()


class MockTcpServer:

    def __init__(self, service_port, mocker):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.service_port = service_port
        self.mocker = mocker
        self.connected = False
        self.errors = []
        self.join_already_failed = False
        self.stopped = False
        self.instructions = []
        self.server_event_queue = asyncio.Queue()
        self.server_actions = asyncio.Queue()
        self.expecations_queue = asyncio.Queue()

        self.evaluator_task = None
        self.server = None
        self.reader = None
        self.writer = None
        self.client_reader = None
        self.client_writer = None

        self.client_called_writer_waited_closed = asyncio.Event()
        self.data_read_by_client = b""
        self.data_sent_from_server = b""

    def protocol_factory(self, original_protocol):
        return InterceptorProtocol(self, original_protocol)

    def register_client_streams(self, client_reader, client_writer):
        if self.client_reader is not None:
            return

        self.client_reader = client_reader
        self.client_writer = client_writer

        self.original_client_writer_close = self.client_writer.close
        self.mocker.patch.object(self.client_writer, "close", self.client_writer_close)

        self.original_client_writer_wait_closed = self.client_writer.wait_closed
        self.mocker.patch.object(self.client_writer, "wait_closed", self.client_writer_wait_closed)

        self.original_client_reader_read = self.client_reader.read
        self.mocker.patch.object(self.client_reader, "read", self.client_read)

    async def client_read(self, *args, **kwargs):
        data = await self.original_client_reader_read(*args, **kwargs)
        self.data_read_by_client += data
        return data

    def client_writer_close(self):
        self.logger.debug("client_writer_close")
        self.original_client_writer_close()

    async def client_writer_wait_closed(self):
        self.logger.debug("client_writer_wait_closed")
        self.client_called_writer_waited_closed.set()
        await self.original_client_writer_wait_closed()

    async def start(self):
        self.evaluator_task = asyncio.create_task(self.evaluate_expectations())
        self.server_action_task = asyncio.create_task(self.execute_server_actions())

        # I thought it would be neater to have `ExpectConnect.server_action`
        # method call `start_accepting_connections` but then there's a race
        # between the server starting to accept connections and the test client
        # actually making the connection. If the client wins, there's no server
        # waiting on the port and connection attempt fails. I tried it and the client
        # usually wins.
        #
        # In fact, there is no guarantee that the client will call
        # `expect_connect` _before_ actually attempting the connection. It may
        # try the connection and then call `expect_connect`. We want that to
        # work. So we have to guarantee that the server is already accepting
        # connections by the time the test is invoked with the `tcpserver`
        # fixture.
        await self.start_accepting_connections()

    async def start_accepting_connections(self):

        def handle_client_connection(reader, writer):
            if self.connected:
                self.server_event_queue.put_nowait(SecondClientConnectionAttempted())
                return
            self.connected = True
            self.reader = reader
            self.writer = writer
            self.server_event_queue.put_nowait(ClientConnectedEvent())

        self.server = await asyncio.start_server(
            handle_client_connection,
            port=self.service_port,
            start_serving=True,
        )

    async def evaluate_expectations(self):
        while True:

            # If there are already errors, there's no point evaluating the expectation.
            # However, we do still have to call `task_done` on the queue to
            # signal that the expectation has been processed.

            expectation = await self.expecations_queue.get()
            self.logger.debug("expectation=%s", expectation)
            if not self.errors:
                # Asynchronously, we want to generate the server event that corresponds to
                # this expectation. We have to do it asynchronously because there may already
                # be other actions from previous expectations. If everything goes well, the
                # call to `evaluate` will match up with the event generated by the server
                # action.
                self.logger.debug("server_action: %s", expectation.server_action.__self__)
                self.server_actions.put_nowait(expectation.server_action)
                try:
                    await expectation.evaluate()
                except Exception as e:
                    self.logger.debug("evaluate() raise exception: %s", e)
                    self.error(e)
            else:
                self.logger.debug("There are errors, expectation not evaluated")
            self.expecations_queue.task_done()

    async def execute_server_actions(self):
        while True:
            server_action = await self.server_actions.get()
            if not self.errors:
                self.logger.debug("no errors, executing server action %s", server_action.__self__)
                try:
                    server_event = await server_action()
                except Exception as e:
                    self.error(e)
                    server_event = ExceptionEvent(e)
                self.logger.debug("server_action=%s, server_event=%s", server_action.__self__, server_event)
                if server_event is not None:
                    self.server_event_queue.put_nowait(server_event)
            else:
                self.logger.debug("There are errors, dropping server action %s", server_action.__self__)

    def error(self, exception):
        self.errors.append(exception)

    async def stop(self):
        try:
            await self.join()
        finally:
            self.stopped = True
            # Cancel evaluator_task
            self.evaluator_task.cancel()
            try:
                await self.evaluator_task
            except asyncio.CancelledError:
                pass

            # Cancel server_action_task
            self.server_action_task.cancel()
            try:
                await self.server_action_task
            except asyncio.CancelledError:
                pass

            self.server.close()
            await self.server.wait_closed()

    async def join(self):

        if self.join_already_failed:
            return

        # Wait for all expectations to be completed, which includes failure
        await self.expecations_queue.join()

        self.logger.debug("self.errors=%s", self.errors)
        if self.errors:
            self.join_already_failed = True
            raise Exception(interpret_error(self.errors[0]))

    def check_not_stopped(self):
        if self.stopped:
            raise Exception("Fixture is stopped")

    def expect_connect(self, timeout=1):
        self.check_not_stopped()
        self.expecations_queue.put_nowait(ExpectConnect(self, timeout=timeout))

    def expect_bytes(self, expected_bytes, timeout=1):
        self.check_not_stopped()
        self.expecations_queue.put_nowait(ExpectBytes(
            self, expected_bytes=expected_bytes, timeout=timeout
        ))

    def send_bytes(self, data):
        self.check_not_stopped()
        self.expecations_queue.put_nowait(SendBytes(self, data))

    def expect_disconnect(self, timeout=1):
        self.check_not_stopped()
        self.expecations_queue.put_nowait(ExpectReadZeroBytes(self, timeout))
        self.expecations_queue.put_nowait(ExpectClientCalledWriterWaitClosed(self, timeout))
        self.expecations_queue.put_nowait(ExpectClientReadAllSentBytes(self, timeout))


class MockTcpServerFactory:

    def __init__(self, unused_tcp_port_factory, mocker):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.unused_tcp_port_factory = unused_tcp_port_factory
        self.mocker = mocker
        self.servers = {}
        self.original_open_connection = asyncio.open_connection
        self.mocker.patch(
            "asyncio.open_connection",
            self.intercept_open_connection
        )
        self.orignal_create_connection = asyncio.get_event_loop().create_connection
        self.mocker.patch.object(
            asyncio.get_event_loop(),
            "create_connection",
            self.intercept_create_connection
        )

    async def __call__(self):
        server = MockTcpServer(self.unused_tcp_port_factory(), self.mocker)
        await server.start()
        self.servers[server.service_port] = server
        return server

    async def intercept_open_connection(self, host, port):
        if host is not None:
            raise Exception(f"`host` parameter to `open_connection` should be `None` but it is {host}")
        client_reader, client_writer = await self.original_open_connection(host, port)
        server = self.servers[port]
        server.register_client_streams(client_reader, client_writer)
        return client_reader, client_writer

    async def intercept_create_connection(
        self, protocol_factory, host, port, *args, **kwargs
    ):
        server = self.servers[port]

        def factory():
            return server.protocol_factory(protocol_factory())

        return await self.orignal_create_connection(
            factory, host, port, *args, **kwargs
        )

    async def stop(self):
        errors = []
        for server in self.servers.values():
            try:
                if not server.join_already_failed:
                    server.expect_disconnect()
                await server.stop()
            except Exception as e:
                errors.append(e)
        if errors:
            raise errors[0]


@pytest.fixture
async def tcpserver_factory(unused_tcp_port_factory, mocker):
    factory = MockTcpServerFactory(unused_tcp_port_factory, mocker)
    yield factory
    await factory.stop()


@pytest.fixture
async def tcpserver(tcpserver_factory):
    return await tcpserver_factory()
