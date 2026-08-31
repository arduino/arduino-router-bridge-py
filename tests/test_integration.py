# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import os
import queue
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import msgpack

from arduino.router_bridge import shutdown
from arduino.router_bridge.bridge import ClientServer
from arduino.router_bridge.connection import BUFFER_LIMIT_EXCEEDED_ERR


class TestIntegration(unittest.TestCase):
    def setUp(self):
        """Set up for each test. Resets the singleton and creates a temporary
        directory for the Unix socket.
        """
        shutdown()  # Forget any shared connection left over from other tests

        self.tmpdir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmpdir.name, "test.sock")
        self.stop_server = threading.Event()
        self.server_thread = None

        # Patch dependencies
        # Mock the logger used by ClientServer
        logger_patcher = patch("arduino.router_bridge.connection.logger", MagicMock())
        logger_patcher.start()
        self.addCleanup(logger_patcher.stop)

    def tearDown(self):
        """Clean up after each test by stopping the server thread and removing
        the temporary directory.
        """
        self.stop_server.set()

        # Make a dummy connection to unblock server.accept() if it's waiting
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                s.connect(self.socket_path)
        except Exception:
            pass  # This is fine, the server might already be closed.

        if self.server_thread:
            self.server_thread.join(timeout=2)

        shutdown()  # Stop all shared connections created by the test

        self.tmpdir.cleanup()

    def test_notify(self):
        """Tests that ClientServer.notify correctly sends a message to the server."""
        server_ready = threading.Event()
        received_queue = queue.Queue()

        def server_logic():
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_sock.bind(self.socket_path)
            server_sock.listen(1)
            server_ready.set()
            conn, _ = server_sock.accept()
            unpacker = msgpack.Unpacker()
            while not self.stop_server.is_set():
                data = conn.recv(1024)
                if not data:
                    break
                unpacker.feed(data)
                for msg in unpacker:
                    received_queue.put(msg)
            conn.close()
            server_sock.close()

        self.server_thread = threading.Thread(target=server_logic, daemon=True)
        self.server_thread.start()
        self.assertTrue(server_ready.wait(timeout=2), "Server did not become ready")

        client = ClientServer(address=f"unix://{self.socket_path}")
        client.wait_connected(timeout=2)

        client.notify("test_method", "hello", 123)

        try:
            received = received_queue.get(timeout=2)
            self.assertEqual(received, [2, "test_method", ["hello", 123]])
        except queue.Empty:
            self.fail("Server did not receive notify message in time.")

    def test_call(self):
        """Tests that ClientServer.call correctly sends a request and receives a response."""
        server_ready = threading.Event()

        def server_logic():
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_sock.bind(self.socket_path)
            server_sock.listen(1)
            server_ready.set()
            conn, _ = server_sock.accept()
            unpacker = msgpack.Unpacker(raw=False)
            data = conn.recv(1024)
            unpacker.feed(data)
            msg = next(unpacker)

            # Verify request and send response
            self.assertEqual(msg[0], 0)  # type: request
            self.assertEqual(msg[2], "get_value")
            response = [1, msg[1], None, "success!"]
            conn.sendall(msgpack.packb(response))

            self.stop_server.wait()
            conn.close()
            server_sock.close()

        self.server_thread = threading.Thread(target=server_logic, daemon=True)
        self.server_thread.start()
        self.assertTrue(server_ready.wait(timeout=2), "Server did not become ready")

        client = ClientServer(address=f"unix://{self.socket_path}")
        client.wait_connected(timeout=2)

        result = client.call("get_value")
        self.assertEqual(result, "success!")

    def test_call_pending_when_router_disconnects(self):
        """Tests that a pending call raises ConnectionError when the router drops the connection."""
        server_ready = threading.Event()

        def server_logic():
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_sock.bind(self.socket_path)
            server_sock.listen(1)
            server_ready.set()
            conn, _ = server_sock.accept()
            conn.recv(1024)  # Wait for the request, then drop the connection without replying
            conn.close()
            self.stop_server.wait()
            server_sock.close()

        self.server_thread = threading.Thread(target=server_logic, daemon=True)
        self.server_thread.start()
        self.assertTrue(server_ready.wait(timeout=2), "Server did not become ready")

        client = ClientServer(address=f"unix://{self.socket_path}")
        client.wait_connected(timeout=2)

        with self.assertRaises(ConnectionError):
            client.call("some_method", timeout=5)

    def test_provide(self):
        """Tests that ClientServer.provide makes a function callable by the server."""
        server_ready = threading.Event()
        response_queue = queue.Queue()

        def server_logic():
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_sock.bind(self.socket_path)
            server_sock.listen(1)
            server_ready.set()
            conn, _ = server_sock.accept()
            unpacker = msgpack.Unpacker(raw=False)

            # 1. Receive $/register call from client
            data = conn.recv(1024)
            unpacker.feed(data)
            register_msg = next(unpacker)
            self.assertEqual(register_msg[0], 0)
            self.assertEqual(register_msg[2], "$/register")
            self.assertEqual(register_msg[3], ["add"])

            # 2. Send a success response for the registration
            reg_response = [1, register_msg[1], None, None]
            conn.sendall(msgpack.packb(reg_response))

            # Give the client a moment to process the registration response
            time.sleep(0.5)

            # 3. Send a request to the client to call the provided function
            call_msg = [0, 123, "add", [10, 5]]
            conn.sendall(msgpack.packb(call_msg))

            # 4. Wait for the client's response
            data = conn.recv(1024)
            unpacker.feed(data)
            response_msg = next(unpacker)
            response_queue.put(response_msg)

            self.stop_server.wait()
            conn.close()
            server_sock.close()

        self.server_thread = threading.Thread(target=server_logic, daemon=True)
        self.server_thread.start()
        self.assertTrue(server_ready.wait(timeout=2), "Server did not become ready")

        client = ClientServer(address=f"unix://{self.socket_path}")
        client.wait_connected(timeout=2)

        client.provide("add", lambda a, b: a + b)

        try:
            final_response = response_queue.get(timeout=2)
            # [1, 123, None, 15]
            self.assertEqual(final_response[0], 1)  # type: response
            self.assertEqual(final_response[1], 123)  # msgid
            self.assertIsNone(final_response[2])  # error
            self.assertEqual(final_response[3], 15)  # result
        except queue.Empty:
            self.fail("Server did not receive response for provided method.")

    def test_handler_can_call_back_into_the_bridge(self):
        """Tests that a provided handler can itself perform a bridge call: handlers must
        run off the read thread, or the nested call's response could never be processed."""
        server_ready = threading.Event()
        response_queue = queue.Queue()

        def server_logic():
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_sock.bind(self.socket_path)
            server_sock.listen(1)
            server_ready.set()
            conn, _ = server_sock.accept()
            unpacker = msgpack.Unpacker(raw=False)

            def read_msg():
                for msg in unpacker:
                    return msg
                while True:
                    data = conn.recv(4096)
                    if not data:
                        raise ConnectionError("Client disconnected")
                    unpacker.feed(data)
                    for msg in unpacker:
                        return msg

            # 1. Acknowledge the $/register call issued by provide()
            register_msg = read_msg()
            self.assertEqual(register_msg[2], "$/register")
            conn.sendall(msgpack.packb([1, register_msg[1], None, None]))

            # 2. Call the provided "compound" method on the client
            conn.sendall(msgpack.packb([0, 7, "compound", [5]]))

            # 3. The client handler calls "double" back on us: answer it
            double_msg = read_msg()
            self.assertEqual(double_msg[0], 0)
            self.assertEqual(double_msg[2], "double")
            conn.sendall(msgpack.packb([1, double_msg[1], None, double_msg[3][0] * 2]))

            # 4. Collect the client's response to the original "compound" request
            response_queue.put(read_msg())

            self.stop_server.wait()
            conn.close()
            server_sock.close()

        self.server_thread = threading.Thread(target=server_logic, daemon=True)
        self.server_thread.start()
        self.assertTrue(server_ready.wait(timeout=2), "Server did not become ready")

        client = ClientServer(address=f"unix://{self.socket_path}")
        client.wait_connected(timeout=2)

        client.provide("compound", lambda x: client.call("double", x, timeout=5) + 1)

        try:
            final_response = response_queue.get(timeout=5)
            self.assertEqual(final_response, [1, 7, None, 11])  # double(5) + 1
        except queue.Empty:
            self.fail("Handler calling back into the bridge did not complete.")

    def test_reconnection(self):
        """Tests that the client automatically reconnects after the server disconnects it."""
        connections = []
        server_ready = threading.Event()

        def server_logic():
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_sock.bind(self.socket_path)
            server_sock.listen(1)
            server_ready.set()

            # Accept first connection and close it immediately
            conn1, _ = server_sock.accept()
            connections.append(conn1)
            conn1.close()

            # Accept the second (reconnected) connection
            conn2, _ = server_sock.accept()
            connections.append(conn2)

            self.stop_server.wait()  # Keep connection open until test ends
            conn2.close()
            server_sock.close()

        self.server_thread = threading.Thread(target=server_logic, daemon=True)
        self.server_thread.start()
        self.assertTrue(server_ready.wait(timeout=2), "Server did not become ready")

        with patch("arduino.router_bridge.connection._reconnect_delay", 0):  # Speed up reconnection for the test
            ClientServer(address=f"unix://{self.socket_path}")

            time_waited = 0
            while len(connections) < 2 and time_waited < 5:
                time.sleep(0.1)
                time_waited += 0.1

            self.assertEqual(len(connections), 2, "Client did not reconnect in time")

    def test_router_returns_buffer_limit_error(self):
        """Simulate router rejecting a request due to buffer limit and verify ClientServer propagates the error."""
        server_ready = threading.Event()

        def server_logic():
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_sock.bind(self.socket_path)
            server_sock.listen(1)
            server_ready.set()
            conn, _ = server_sock.accept()
            unpacker = msgpack.Unpacker(raw=False)
            data = conn.recv(4096)
            unpacker.feed(data)
            req = next(unpacker)

            # Send a response containing the buffer-limit error
            resp = [1, req[1], [BUFFER_LIMIT_EXCEEDED_ERR, f"message size exceeds the limit of {128} bytes"], None]
            conn.sendall(msgpack.packb(resp))

            self.stop_server.wait()
            conn.close()
            server_sock.close()

        self.server_thread = threading.Thread(target=server_logic, daemon=True)
        self.server_thread.start()
        self.assertTrue(server_ready.wait(timeout=2), "Server did not become ready")

        client = ClientServer(address=f"unix://{self.socket_path}")
        client.wait_connected(timeout=2)

        with self.assertRaises(ValueError) as cm:
            client.call("some_method")

        self.assertIn("message size exceeds the limit", str(cm.exception))
