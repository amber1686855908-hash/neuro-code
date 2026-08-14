/*
 * Acceptance-only W3 Winsock probe.
 *
 * This source is compiled on a trusted Windows CI controller and copied into
 * a disposable sandbox workspace.  It is deliberately not part of the Neuro
 * Code package or production runtime.
 */

#define WIN32_LEAN_AND_MEAN

#include <winsock2.h>
#include <ws2tcpip.h>

#include <stdio.h>

static int emit_result(const char *stage, int connected, int wsa_error) {
    const char *connected_text = connected ? "true" : "false";
    (void)printf(
        "W3_WINSOCK={\"stage\":\"%s\",\"connected\":%s,\"wsa_error\":%d}\n",
        stage,
        connected_text,
        wsa_error
    );
    (void)fflush(stdout);
    return connected ? 0 : 1;
}

int main(void) {
    WSADATA wsa_data;
    int result = WSAStartup(MAKEWORD(2, 2), &wsa_data);
    if (result != 0) {
        return emit_result("WSA_STARTUP", 0, result);
    }

    SOCKET socket_handle = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (socket_handle == INVALID_SOCKET) {
        int error = WSAGetLastError();
        (void)WSACleanup();
        return emit_result("SOCKET", 0, error);
    }

    struct sockaddr_in endpoint;
    (void)ZeroMemory(&endpoint, sizeof(endpoint));
    endpoint.sin_family = AF_INET;
    endpoint.sin_port = htons(80);
    if (InetPtonA(AF_INET, "1.1.1.1", &endpoint.sin_addr) != 1) {
        int error = WSAGetLastError();
        (void)closesocket(socket_handle);
        (void)WSACleanup();
        return emit_result("CONNECT", 0, error);
    }

    u_long nonblocking = 1;
    if (ioctlsocket(socket_handle, FIONBIO, &nonblocking) == SOCKET_ERROR) {
        int error = WSAGetLastError();
        (void)closesocket(socket_handle);
        (void)WSACleanup();
        return emit_result("CONNECT", 0, error);
    }

    result = connect(
        socket_handle,
        (const struct sockaddr *)&endpoint,
        (int)sizeof(endpoint)
    );
    if (result == 0) {
        (void)closesocket(socket_handle);
        (void)WSACleanup();
        return emit_result("CONNECT", 1, 0);
    }

    int connect_error = WSAGetLastError();
    if (
        connect_error != WSAEWOULDBLOCK
        && connect_error != WSAEINPROGRESS
        && connect_error != WSAEALREADY
    ) {
        (void)closesocket(socket_handle);
        (void)WSACleanup();
        return emit_result("CONNECT", 0, connect_error);
    }

    fd_set writable;
    fd_set exceptional;
    FD_ZERO(&writable);
    FD_ZERO(&exceptional);
    FD_SET(socket_handle, &writable);
    FD_SET(socket_handle, &exceptional);
    struct timeval timeout;
    timeout.tv_sec = 5;
    timeout.tv_usec = 0;

    result = select(0, NULL, &writable, &exceptional, &timeout);
    if (result == 0) {
        connect_error = WSAETIMEDOUT;
    } else if (result == SOCKET_ERROR) {
        connect_error = WSAGetLastError();
    } else {
        int socket_error = 0;
        int socket_error_size = (int)sizeof(socket_error);
        if (
            getsockopt(
                socket_handle,
                SOL_SOCKET,
                SO_ERROR,
                (char *)&socket_error,
                &socket_error_size
            ) == SOCKET_ERROR
        ) {
            connect_error = WSAGetLastError();
        } else {
            connect_error = socket_error;
        }
    }

    (void)closesocket(socket_handle);
    (void)WSACleanup();
    if (connect_error == 0) {
        return emit_result("CONNECT", 1, 0);
    }
    return emit_result("CONNECT", 0, connect_error);
}
