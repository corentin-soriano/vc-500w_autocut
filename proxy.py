#!/bin/python3
#
# vc-500w_autocut  Copyright (C) 2024  Corentin SORIANO
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

import signal
import socket
import sys
import threading
from select import select

# Configuration of remote printer.
PRINTER_IP     = 'vc-500w.host'
PRINTER_PORT   = 9100

# Local proxy listening.
LISTER_ADDR    = '127.0.0.1'
LISTEN_PORT    = 9100

# Data limits.
MAX_DATA_IMG   = 20000000 # 20 Mo
MAX_DATA_XML   = 50000    # 50 ko

# Timeout in select().
SELECT_TIMEOUT = 1.0


def handle_exit(*args):
    """
    Handle the graceful exit of the application by tcp sockets.
    This function is intended to be used as a signal handler for codes
    SIGINT (Ctrl+C) or SIGTERM.

    Args:
        *args: Variable length argument list. This is typically used to
               pass signal number and frame information when the function
               is used as a signal handler.

    Raises:
        SystemExit: This function will terminate the program by raising
                    SystemExit with a status code of 0.
    """

    # Send stop event to threads.
    stop_event.set()

    # Wait for threads end.
    listener_thread.join()

    # Exit python application.
    sys.exit(0)


def modify_xml(data):
    """
    Modifies the XML content if certain conditions are met.

    This function takes an XML byte string as input and modifies its content
    by adding a `<cutmode>full</cutmode>` tag before the closing `</print>` tag,
    provided the length of the data is less than a maximum value defined by
    `MAX_DATA_XML`. If the length of the data exceeds this value, the function
    assumes the data represents an image and does not modify it.

    Args:
        data (bytes): The XML content to process.

    Returns:
        bytes: The potentially modified XML content.
    """

    # Don't log or process picture content.
    if len(data) > MAX_DATA_XML:
        print('Picture received.')
        return data

    # Log xml content.
    print(f'XML content: {data}')

    # Find </print> position.
    end_tag_pos = data.find(b'</print>')

    # No </print> in xml.
    if end_tag_pos == -1:
        return data

    # Add new line with cut mode before </print>.
    new_line = b'<cutmode>full</cutmode>\n'
    new_data = data[:end_tag_pos] + new_line + data[end_tag_pos:]
    print(f'XML modified: {new_data}')
    return new_data


def socket_read(sock, max_bytes, stop_event):
    """
    Reads data from a socket after brief wait.

    This function attempts to read a specified maximum number of bytes from a
    given socket after waiting briefly to allow the printer to complete its
    data transmission.

    Args:
        sock (socket.socket): The socket from which to read data.
        max_bytes (int): The maximum number of bytes to read from the socket.
        stop_event (threading.Event): An event object to check if a stop is set.

    Returns:
        bytes: The data read from the socket.
    """

    stop_event.wait(0.5)
    return sock.recv(max_bytes)


def socket_write(sock, data):
    """
    Sends data through a socket.

    Args:
        sock (socket.socket): The socket through which to send data.
        data (bytes): The data to be sent through the socket.
    """

    sock.sendall(data)


def socket_close(close_socket):
    """
    Gracefully closes a socket connection.

    This function shuts down the socket's by sending FIN signal and wait for
    ACK from remote host.

    Args:
        close_socket (socket.socket): The socket to be closed.
    """

    # Send FIN signal to remote host.
    close_socket.shutdown(socket.SHUT_WR)

    # Wait for ACK.
    while True:
        data = close_socket.recv(1024)
        if not data:
            break

    # Close the socket.
    close_socket.close()


def socket_wait_readable(socket_list, stop_event, timeout):
    """
    Waits for one or more sockets to become readable, with a stop event check.

    This function waits for sockets in the provided list to become readable,
    periodically checking if a stop event is set. It returns the list of readable
    sockets as soon as at least one socket is readable.

    Args:
        socket_list (list of socket.socket): The list of sockets to monitor for readability.
        stop_event (threading.Event): An event object to check if a stop is signaled.
        timeout (float): The timeout period in seconds for each select call.

    Returns:
        list of socket.socket: The list of sockets that are readable.
    """

    outputs = []

    while not stop_event.is_set():
        readable, _, _ = select(socket_list, outputs, socket_list, timeout)

        if readable:
            return readable


def client_thread(client_socket, stop_event):
    """
    Thread function handling communication between a client and a printer socket.

    This function manages a bidirectional communication channel between a client
    socket and a printer socket. It reads data from the client socket, optionally
    modifies it, and forwards it to the printer socket. It continues this process
    until either a stop event is set or a termination signal (FIN) is received
    from either socket.

    Args:
        client_socket (socket.socket): The client socket connected to a client.
        stop_event (threading.Event): An event object to check if a stop is signaled.
    """

    with client_socket:

        # Open a new socket for the printer.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as printer_socket:
            printer_socket.connect((PRINTER_IP, PRINTER_PORT))

            # List of sockets to monitor for readability.
            inputs = [
                client_socket,
                printer_socket
            ]

            # Flag to signal when to exit the loop.
            exit_flag = False

            # Exchange data between client and printer sockets.
            while not stop_event.is_set() and not exit_flag:

                # Wait for either socket to become readable.
                readable = socket_wait_readable(inputs, stop_event, SELECT_TIMEOUT)

                # Process each readable socket.
                for sock in readable:

                    # Determine maximum bytes to read based on the socket type.
                    max_bytes = MAX_DATA_IMG if sock is client_socket else MAX_DATA_XML

                    # Read data from the socket.
                    data = socket_read(sock, max_bytes, stop_event)

                    # Received FIN signal, set exit flag.
                    if not data:
                        exit_flag = True
                        break

                    # Modify XML data.
                    data = modify_xml(data)

                    # Determine destination socket and write data to it.
                    sock_dst = printer_socket if sock is client_socket else client_socket
                    socket_write(sock_dst, data)

            # Close printer socket gracefully.
            socket_close(printer_socket)

        # Close client socket gracefully.
        socket_close(client_socket)


def listener_thread(stop_event):
    """
    Thread function that listens for incoming connections and spawns client threads.

    This function creates a socket to listen for incoming connections on a specified
    address and port. It accepts incoming connections and spawns a new thread for
    each client to handle bidirectional communication between the client and a printer.
    The function continues to listen for new connections until a stop event is set.

    Args:
        stop_event (threading.Event): An event object to check if a stop is signaled.
    """

    # Create a socket for listening to incoming connections.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((LISTER_ADDR, LISTEN_PORT))
        listener.listen(5)
        print(f'Listening on port {LISTEN_PORT}...')

        # Client thread storage.
        client_thread_list = []

        # Stay in the listener loop until the stop event is set.
        while not stop_event.is_set():

            # Wait for incoming connection requests.
            socket_wait_readable([listener], stop_event, SELECT_TIMEOUT)

            # Stop event, exit listener loop.
            if stop_event.is_set():
                break

            # Accept incomming connection.
            client_socket, client_address = listener.accept()
            print(f'Connection from {client_address}')

            # Create a new thread to handle communication with the client.
            thread = threading.Thread(target=client_thread,
                                      args=(client_socket,stop_event))
            thread.start()
            client_thread_list.append(thread)

            # Clean up old threads that have terminated.
            client_thread_list = [
                thread for thread in client_thread_list if thread.is_alive()
            ]
        
        # Wait for all client threads to terminate.
        for thread in client_thread_list:
            thread.join()

        # Close listener socket.
        listener.close()


if __name__ == '__main__':

    # Register signal handlers.
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # New thread for listener.
    stop_event = threading.Event()
    listener_thread = threading.Thread(target=listener_thread,args=(stop_event,))
    listener_thread.start()

    # Wait for listener thread to end.
    listener_thread.join()
