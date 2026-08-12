#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>
#include <ws2tcpip.h>
#include <sddl.h>
#include <userenv.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#define FRAME_HELLO ((BYTE)'H')
#define FRAME_TARGET ((BYTE)'T')
#define FRAME_DATA ((BYTE)'D')
#define FRAME_EOF ((BYTE)'E')
#define FRAME_STDOUT ((BYTE)'O')
#define FRAME_STDERR ((BYTE)'R')
#define FRAME_EXIT ((BYTE)'X')
#define FRAME_READY ((BYTE)'Q')
#define MAX_FRAME (1024U * 1024U)

typedef struct CHILD_PIPES {
    HANDLE stdin_write;
    HANDLE stdout_read;
    HANDLE stderr_read;
    HANDLE facts_read;
    PROCESS_INFORMATION process;
} CHILD_PIPES;

static BOOL write_all(HANDLE handle, const void *source, DWORD length) {
    const BYTE *cursor = (const BYTE *)source;
    DWORD remaining = length;
    while (remaining > 0) {
        DWORD written = 0;
        DWORD chunk = remaining > 3071 ? 3071 : remaining;
        if (!WriteFile(handle, cursor, chunk, &written, NULL) || written == 0) {
            return FALSE;
        }
        cursor += written;
        remaining -= written;
    }
    return TRUE;
}

static BOOL read_exact(HANDLE handle, void *destination, DWORD length) {
    BYTE *cursor = (BYTE *)destination;
    DWORD remaining = length;
    while (remaining > 0) {
        DWORD received = 0;
        if (!ReadFile(handle, cursor, remaining, &received, NULL) || received == 0) {
            return FALSE;
        }
        cursor += received;
        remaining -= received;
    }
    return TRUE;
}

static BOOL send_frame(HANDLE handle, BYTE kind, const void *payload, DWORD length) {
    BYTE header[5];
    header[0] = kind;
    header[1] = (BYTE)(length & 0xffU);
    header[2] = (BYTE)((length >> 8) & 0xffU);
    header[3] = (BYTE)((length >> 16) & 0xffU);
    header[4] = (BYTE)((length >> 24) & 0xffU);
    return write_all(handle, header, sizeof(header)) &&
           (length == 0 || write_all(handle, payload, length));
}

static BOOL receive_frame(HANDLE handle, BYTE *kind, BYTE **payload, DWORD *length) {
    BYTE header[5];
    *payload = NULL;
    *length = 0;
    if (!read_exact(handle, header, sizeof(header))) {
        return FALSE;
    }
    *kind = header[0];
    *length = (DWORD)header[1] | ((DWORD)header[2] << 8) |
              ((DWORD)header[3] << 16) | ((DWORD)header[4] << 24);
    if (*length > MAX_FRAME) {
        SetLastError(ERROR_BUFFER_OVERFLOW);
        return FALSE;
    }
    if (*length == 0) {
        return TRUE;
    }
    *payload = (BYTE *)HeapAlloc(GetProcessHeap(), 0, *length);
    if (*payload == NULL) {
        SetLastError(ERROR_NOT_ENOUGH_MEMORY);
        return FALSE;
    }
    if (!read_exact(handle, *payload, *length)) {
        HeapFree(GetProcessHeap(), 0, *payload);
        *payload = NULL;
        return FALSE;
    }
    return TRUE;
}

static BOOL read_all(HANDLE handle, BYTE **payload, DWORD *length) {
    DWORD capacity = 65536;
    DWORD used = 0;
    BYTE *buffer = (BYTE *)HeapAlloc(GetProcessHeap(), 0, capacity);
    if (buffer == NULL) {
        return FALSE;
    }
    for (;;) {
        DWORD received = 0;
        if (used == capacity) {
            DWORD next = capacity * 2;
            BYTE *grown;
            if (next > MAX_FRAME) {
                HeapFree(GetProcessHeap(), 0, buffer);
                SetLastError(ERROR_BUFFER_OVERFLOW);
                return FALSE;
            }
            grown = (BYTE *)HeapReAlloc(GetProcessHeap(), 0, buffer, next);
            if (grown == NULL) {
                HeapFree(GetProcessHeap(), 0, buffer);
                return FALSE;
            }
            buffer = grown;
            capacity = next;
        }
        if (!ReadFile(handle, buffer + used, capacity - used, &received, NULL)) {
            DWORD error = GetLastError();
            if (error == ERROR_BROKEN_PIPE || error == ERROR_NO_DATA) {
                break;
            }
            HeapFree(GetProcessHeap(), 0, buffer);
            return FALSE;
        }
        if (received == 0) {
            break;
        }
        used += received;
    }
    *payload = buffer;
    *length = used;
    return TRUE;
}

static BOOL read_line(HANDLE handle, BYTE **payload, DWORD *length) {
    DWORD capacity = 4096;
    DWORD used = 0;
    BYTE *buffer = (BYTE *)HeapAlloc(GetProcessHeap(), 0, capacity);
    if (buffer == NULL) {
        return FALSE;
    }
    for (;;) {
        BYTE value;
        DWORD received = 0;
        if (!ReadFile(handle, &value, 1, &received, NULL)) {
            HeapFree(GetProcessHeap(), 0, buffer);
            return FALSE;
        }
        if (received == 0) {
            HeapFree(GetProcessHeap(), 0, buffer);
            SetLastError(ERROR_BROKEN_PIPE);
            return FALSE;
        }
        if (used == capacity) {
            BYTE *grown;
            if (capacity >= MAX_FRAME) {
                HeapFree(GetProcessHeap(), 0, buffer);
                SetLastError(ERROR_BUFFER_OVERFLOW);
                return FALSE;
            }
            capacity *= 2;
            grown = (BYTE *)HeapReAlloc(GetProcessHeap(), 0, buffer, capacity);
            if (grown == NULL) {
                HeapFree(GetProcessHeap(), 0, buffer);
                return FALSE;
            }
            buffer = grown;
        }
        buffer[used++] = value;
        if (value == '\n') {
            break;
        }
    }
    *payload = buffer;
    *length = used;
    return TRUE;
}

static BOOL token_value(TOKEN_INFORMATION_CLASS kind, void **buffer) {
    HANDLE token = NULL;
    DWORD needed = 0;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        return FALSE;
    }
    GetTokenInformation(token, kind, NULL, 0, &needed);
    *buffer = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, needed);
    if (*buffer == NULL ||
        !GetTokenInformation(token, kind, *buffer, needed, &needed)) {
        if (*buffer != NULL) {
            HeapFree(GetProcessHeap(), 0, *buffer);
            *buffer = NULL;
        }
        CloseHandle(token);
        return FALSE;
    }
    CloseHandle(token);
    return TRUE;
}

static void token_facts(char *output, size_t capacity) {
    DWORD is_appcontainer = 0;
    DWORD returned = 0;
    DWORD integrity = 0;
    HANDLE token = NULL;
    TOKEN_APPCONTAINER_INFORMATION *container = NULL;
    TOKEN_MANDATORY_LABEL *label = NULL;
    LPWSTR sid_text = NULL;
    BOOL in_job = FALSE;
    char sid_utf8[512] = "";

    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        GetTokenInformation(
            token, TokenIsAppContainer, &is_appcontainer, sizeof(is_appcontainer), &returned);
        CloseHandle(token);
    }
    if (token_value(TokenAppContainerSid, (void **)&container)) {
        if (container->TokenAppContainer != NULL &&
            ConvertSidToStringSidW(container->TokenAppContainer, &sid_text)) {
            WideCharToMultiByte(
                CP_UTF8, 0, sid_text, -1, sid_utf8, (int)sizeof(sid_utf8), NULL, NULL);
            LocalFree(sid_text);
        }
        HeapFree(GetProcessHeap(), 0, container);
    }
    if (token_value(TokenIntegrityLevel, (void **)&label)) {
        UCHAR count = *GetSidSubAuthorityCount(label->Label.Sid);
        if (count > 0) {
            integrity = *GetSidSubAuthority(label->Label.Sid, count - 1);
        }
        HeapFree(GetProcessHeap(), 0, label);
    }
    IsProcessInJob(GetCurrentProcess(), NULL, &in_job);
    _snprintf_s(
        output,
        capacity,
        _TRUNCATE,
        "{\"pid\":%lu,\"token_is_appcontainer\":%s,\"appcontainer_sid\":\"%s\","
        "\"integrity_rid\":%lu,\"in_job\":%s}",
        GetCurrentProcessId(),
        is_appcontainer ? "true" : "false",
        sid_utf8,
        integrity,
        in_job ? "true" : "false");
}

static HANDLE connect_local_pipe(const wchar_t *name) {
    ULONGLONG deadline = GetTickCount64() + 15000;
    while (GetTickCount64() < deadline) {
        HANDLE pipe = CreateFileW(
            name,
            GENERIC_READ | GENERIC_WRITE,
            0,
            NULL,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            NULL);
        if (pipe != INVALID_HANDLE_VALUE) {
            return pipe;
        }
        if (GetLastError() != ERROR_PIPE_BUSY) {
            return INVALID_HANDLE_VALUE;
        }
        WaitNamedPipeW(name, 250);
    }
    SetLastError(ERROR_SEM_TIMEOUT);
    return INVALID_HANDLE_VALUE;
}

static BOOL make_inheritable_pipe(HANDLE *read_handle, HANDLE *write_handle) {
    SECURITY_ATTRIBUTES security;
    ZeroMemory(&security, sizeof(security));
    security.nLength = sizeof(security);
    security.bInheritHandle = TRUE;
    return CreatePipe(read_handle, write_handle, &security, 0);
}

static BOOL spawn_target(const wchar_t *mode, CHILD_PIPES *child) {
    HANDLE stdin_read = NULL;
    HANDLE stdout_write = NULL;
    HANDLE stderr_write = NULL;
    HANDLE facts_write = NULL;
    SIZE_T attribute_size = 0;
    LPPROC_THREAD_ATTRIBUTE_LIST attributes = NULL;
    HANDLE inherited[4];
    STARTUPINFOEXW startup;
    wchar_t executable[MAX_PATH];
    wchar_t command[32768];
    BOOL created = FALSE;

    ZeroMemory(child, sizeof(*child));
    ZeroMemory(&startup, sizeof(startup));
    startup.StartupInfo.cb = sizeof(startup);
    if (!GetModuleFileNameW(NULL, executable, MAX_PATH) ||
        !make_inheritable_pipe(&stdin_read, &child->stdin_write) ||
        !make_inheritable_pipe(&child->stdout_read, &stdout_write) ||
        !make_inheritable_pipe(&child->stderr_read, &stderr_write) ||
        !make_inheritable_pipe(&child->facts_read, &facts_write)) {
        goto cleanup;
    }
    if (!SetHandleInformation(child->stdin_write, HANDLE_FLAG_INHERIT, 0) ||
        !SetHandleInformation(child->stdout_read, HANDLE_FLAG_INHERIT, 0) ||
        !SetHandleInformation(child->stderr_read, HANDLE_FLAG_INHERIT, 0) ||
        !SetHandleInformation(child->facts_read, HANDLE_FLAG_INHERIT, 0)) {
        goto cleanup;
    }
    InitializeProcThreadAttributeList(NULL, 1, 0, &attribute_size);
    attributes = (LPPROC_THREAD_ATTRIBUTE_LIST)HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY, attribute_size);
    if (attributes == NULL ||
        !InitializeProcThreadAttributeList(attributes, 1, 0, &attribute_size)) {
        goto cleanup;
    }
    inherited[0] = stdin_read;
    inherited[1] = stdout_write;
    inherited[2] = stderr_write;
    inherited[3] = facts_write;
    if (!UpdateProcThreadAttribute(
            attributes,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            inherited,
            sizeof(inherited),
            NULL,
            NULL)) {
        goto cleanup;
    }
    startup.lpAttributeList = attributes;
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = stdin_read;
    startup.StartupInfo.hStdOutput = stdout_write;
    startup.StartupInfo.hStdError = stderr_write;
    if (_snwprintf_s(
            command,
            32768,
            _TRUNCATE,
            L"\"%s\" %s %llu",
            executable,
            mode,
            (unsigned long long)(ULONG_PTR)facts_write) < 0) {
        goto cleanup;
    }
    created = CreateProcessW(
        executable,
        command,
        NULL,
        NULL,
        TRUE,
        EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
        NULL,
        NULL,
        &startup.StartupInfo,
        &child->process);

cleanup:
    if (attributes != NULL) {
        DeleteProcThreadAttributeList(attributes);
        HeapFree(GetProcessHeap(), 0, attributes);
    }
    if (stdin_read != NULL) CloseHandle(stdin_read);
    if (stdout_write != NULL) CloseHandle(stdout_write);
    if (stderr_write != NULL) CloseHandle(stderr_write);
    if (facts_write != NULL) CloseHandle(facts_write);
    if (!created) {
        if (child->stdin_write != NULL) CloseHandle(child->stdin_write);
        if (child->stdout_read != NULL) CloseHandle(child->stdout_read);
        if (child->stderr_read != NULL) CloseHandle(child->stderr_read);
        if (child->facts_read != NULL) CloseHandle(child->facts_read);
        ZeroMemory(child, sizeof(*child));
    }
    return created;
}

static BOOL spawn_command(const wchar_t *command_line, CHILD_PIPES *child) {
    HANDLE stdin_read = NULL;
    HANDLE stdout_write = NULL;
    HANDLE stderr_write = NULL;
    SIZE_T attribute_size = 0;
    LPPROC_THREAD_ATTRIBUTE_LIST attributes = NULL;
    HANDLE inherited[3];
    STARTUPINFOEXW startup;
    wchar_t command[32768];
    BOOL created = FALSE;

    ZeroMemory(child, sizeof(*child));
    ZeroMemory(&startup, sizeof(startup));
    startup.StartupInfo.cb = sizeof(startup);
    if (wcslen(command_line) >= 32767 ||
        wcscpy_s(command, 32768, command_line) != 0 ||
        !make_inheritable_pipe(&stdin_read, &child->stdin_write) ||
        !make_inheritable_pipe(&child->stdout_read, &stdout_write) ||
        !make_inheritable_pipe(&child->stderr_read, &stderr_write)) {
        goto cleanup;
    }
    if (!SetHandleInformation(child->stdin_write, HANDLE_FLAG_INHERIT, 0) ||
        !SetHandleInformation(child->stdout_read, HANDLE_FLAG_INHERIT, 0) ||
        !SetHandleInformation(child->stderr_read, HANDLE_FLAG_INHERIT, 0)) {
        goto cleanup;
    }
    InitializeProcThreadAttributeList(NULL, 1, 0, &attribute_size);
    attributes = (LPPROC_THREAD_ATTRIBUTE_LIST)HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY, attribute_size);
    if (attributes == NULL ||
        !InitializeProcThreadAttributeList(attributes, 1, 0, &attribute_size)) {
        goto cleanup;
    }
    inherited[0] = stdin_read;
    inherited[1] = stdout_write;
    inherited[2] = stderr_write;
    if (!UpdateProcThreadAttribute(
            attributes,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            inherited,
            sizeof(inherited),
            NULL,
            NULL)) {
        goto cleanup;
    }
    startup.lpAttributeList = attributes;
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = stdin_read;
    startup.StartupInfo.hStdOutput = stdout_write;
    startup.StartupInfo.hStdError = stderr_write;
    created = CreateProcessW(
        NULL,
        command,
        NULL,
        NULL,
        TRUE,
        EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
        NULL,
        NULL,
        &startup.StartupInfo,
        &child->process);

cleanup:
    if (attributes != NULL) {
        DeleteProcThreadAttributeList(attributes);
        HeapFree(GetProcessHeap(), 0, attributes);
    }
    if (stdin_read != NULL) CloseHandle(stdin_read);
    if (stdout_write != NULL) CloseHandle(stdout_write);
    if (stderr_write != NULL) CloseHandle(stderr_write);
    if (!created) {
        if (child->stdin_write != NULL) CloseHandle(child->stdin_write);
        if (child->stdout_read != NULL) CloseHandle(child->stdout_read);
        if (child->stderr_read != NULL) CloseHandle(child->stderr_read);
        ZeroMemory(child, sizeof(*child));
    }
    return created;
}

static BOOL send_target_facts(HANDLE pipe, CHILD_PIPES *target) {
    BYTE *facts = NULL;
    DWORD facts_length = 0;
    BOOL ok = read_all(target->facts_read, &facts, &facts_length);
    CloseHandle(target->facts_read);
    target->facts_read = NULL;
    if (!ok) {
        return FALSE;
    }
    ok = send_frame(pipe, FRAME_TARGET, facts, facts_length);
    HeapFree(GetProcessHeap(), 0, facts);
    return ok;
}

static void close_child(CHILD_PIPES *target) {
    if (target->stdin_write != NULL) CloseHandle(target->stdin_write);
    if (target->stdout_read != NULL) CloseHandle(target->stdout_read);
    if (target->stderr_read != NULL) CloseHandle(target->stderr_read);
    if (target->facts_read != NULL) CloseHandle(target->facts_read);
    if (target->process.hThread != NULL) CloseHandle(target->process.hThread);
    if (target->process.hProcess != NULL) CloseHandle(target->process.hProcess);
    ZeroMemory(target, sizeof(*target));
}

static int pipe_denied_mode(const wchar_t *name) {
    HANDLE pipe = CreateFileW(
        name,
        GENERIC_READ | GENERIC_WRITE,
        0,
        NULL,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        NULL);
    if (pipe != INVALID_HANDLE_VALUE) {
        printf("{\"connected\":true,\"error\":0}\n");
        CloseHandle(pipe);
        return 81;
    }
    {
        DWORD error = GetLastError();
        printf("{\"connected\":false,\"error\":%lu}\n", error);
        return error == ERROR_ACCESS_DENIED ? 0 : 82;
    }
}

static int byte_stream_mode(const wchar_t *name) {
    HANDLE pipe = connect_local_pipe(name);
    BYTE kind;
    BYTE *payload = NULL;
    DWORD length = 0;
    char facts[2048];
    if (pipe == INVALID_HANDLE_VALUE) return (int)GetLastError();
    token_facts(facts, sizeof(facts));
    if (!send_frame(pipe, FRAME_HELLO, facts, (DWORD)strlen(facts)) ||
        !receive_frame(pipe, &kind, &payload, &length) || kind != FRAME_DATA ||
        !send_frame(pipe, FRAME_DATA, payload, length)) {
        if (payload != NULL) HeapFree(GetProcessHeap(), 0, payload);
        CloseHandle(pipe);
        return 91;
    }
    if (payload != NULL) HeapFree(GetProcessHeap(), 0, payload);
    FlushFileBuffers(pipe);
    CloseHandle(pipe);
    return 0;
}

static int relay_command_mode(const wchar_t *name, const wchar_t *command_line) {
    HANDLE pipe = connect_local_pipe(name);
    CHILD_PIPES target;
    BYTE kind;
    BYTE *input = NULL;
    DWORD input_length = 0;
    BYTE *stdout_payload = NULL;
    DWORD stdout_length = 0;
    BYTE *stderr_payload = NULL;
    DWORD stderr_length = 0;
    DWORD exit_code = 0;
    char facts[2048];
    char target_json[128];
    int target_json_length;
    BOOL target_in_job = FALSE;
    int result = 95;

    ZeroMemory(&target, sizeof(target));
    if (pipe == INVALID_HANDLE_VALUE) return (int)GetLastError();
    token_facts(facts, sizeof(facts));
    if (!send_frame(pipe, FRAME_HELLO, facts, (DWORD)strlen(facts)) ||
        !spawn_command(command_line, &target)) {
        goto cleanup;
    }
    IsProcessInJob(target.process.hProcess, NULL, &target_in_job);
    target_json_length = _snprintf_s(
        target_json,
        sizeof(target_json),
        _TRUNCATE,
        "{\"pid\":%lu,\"in_job\":%s}",
        target.process.dwProcessId,
        target_in_job ? "true" : "false");
    if (target_json_length < 0 ||
        !send_frame(pipe, FRAME_TARGET, target_json, (DWORD)target_json_length) ||
        !receive_frame(pipe, &kind, &input, &input_length) || kind != FRAME_DATA ||
        !write_all(target.stdin_write, input, input_length)) {
        goto cleanup;
    }
    if (input != NULL) HeapFree(GetProcessHeap(), 0, input);
    input = NULL;
    if (!receive_frame(pipe, &kind, &input, &input_length) || kind != FRAME_EOF) {
        goto cleanup;
    }
    if (input != NULL) HeapFree(GetProcessHeap(), 0, input);
    input = NULL;
    CloseHandle(target.stdin_write);
    target.stdin_write = NULL;
    if (!read_all(target.stdout_read, &stdout_payload, &stdout_length) ||
        !read_all(target.stderr_read, &stderr_payload, &stderr_length) ||
        WaitForSingleObject(target.process.hProcess, 60000) != WAIT_OBJECT_0 ||
        !GetExitCodeProcess(target.process.hProcess, &exit_code) ||
        !send_frame(pipe, FRAME_STDOUT, stdout_payload, stdout_length) ||
        !send_frame(pipe, FRAME_STDERR, stderr_payload, stderr_length) ||
        !send_frame(pipe, FRAME_EXIT, &exit_code, sizeof(exit_code))) {
        goto cleanup;
    }
    result = 0;

cleanup:
    if (input != NULL) HeapFree(GetProcessHeap(), 0, input);
    if (stdout_payload != NULL) HeapFree(GetProcessHeap(), 0, stdout_payload);
    if (stderr_payload != NULL) HeapFree(GetProcessHeap(), 0, stderr_payload);
    close_child(&target);
    if (pipe != INVALID_HANDLE_VALUE) CloseHandle(pipe);
    return result;
}

static int network_pipe_mode(
    const wchar_t *name,
    const wchar_t *host,
    const wchar_t *port) {
    HANDLE pipe = connect_local_pipe(name);
    WSADATA data;
    ADDRINFOW hints;
    PADDRINFOW addresses = NULL;
    PADDRINFOW current;
    SOCKET socket_handle = INVALID_SOCKET;
    int dns_result;
    int connect_result = SOCKET_ERROR;
    int connect_error = 0;
    char facts[2048];
    char result[512];
    int length;
    if (pipe == INVALID_HANDLE_VALUE) return (int)GetLastError();
    token_facts(facts, sizeof(facts));
    if (!send_frame(pipe, FRAME_HELLO, facts, (DWORD)strlen(facts))) {
        CloseHandle(pipe);
        return 96;
    }
    if (WSAStartup(MAKEWORD(2, 2), &data) != 0) {
        CloseHandle(pipe);
        return 97;
    }
    ZeroMemory(&hints, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    dns_result = GetAddrInfoW(host, port, &hints, &addresses);
    if (dns_result == 0) {
        for (current = addresses; current != NULL; current = current->ai_next) {
            socket_handle = socket(current->ai_family, current->ai_socktype, current->ai_protocol);
            if (socket_handle == INVALID_SOCKET) continue;
            connect_result = connect(socket_handle, current->ai_addr, (int)current->ai_addrlen);
            if (connect_result == 0) break;
            connect_error = WSAGetLastError();
            closesocket(socket_handle);
            socket_handle = INVALID_SOCKET;
        }
        FreeAddrInfoW(addresses);
    } else {
        connect_error = dns_result;
    }
    length = _snprintf_s(
        result,
        sizeof(result),
        _TRUNCATE,
        "{\"dns\":%s,\"connected\":%s,\"error\":%d}",
        dns_result == 0 ? "true" : "false",
        connect_result == 0 ? "true" : "false",
        connect_error);
    if (socket_handle != INVALID_SOCKET) closesocket(socket_handle);
    WSACleanup();
    if (length < 0 || !send_frame(pipe, FRAME_DATA, result, (DWORD)length)) {
        CloseHandle(pipe);
        return 98;
    }
    FlushFileBuffers(pipe);
    CloseHandle(pipe);
    return 0;
}

static int relay_stdio_mode(const wchar_t *name) {
    HANDLE pipe = connect_local_pipe(name);
    CHILD_PIPES target;
    BYTE kind;
    BYTE *input = NULL;
    DWORD input_length = 0;
    BYTE *stdout_payload = NULL;
    DWORD stdout_length = 0;
    BYTE *stderr_payload = NULL;
    DWORD stderr_length = 0;
    DWORD exit_code = 0;
    char facts[2048];
    int result = 100;

    ZeroMemory(&target, sizeof(target));
    if (pipe == INVALID_HANDLE_VALUE) return (int)GetLastError();
    token_facts(facts, sizeof(facts));
    if (!send_frame(pipe, FRAME_HELLO, facts, (DWORD)strlen(facts)) ||
        !spawn_target(L"stdio-target", &target) ||
        !send_target_facts(pipe, &target) ||
        !receive_frame(pipe, &kind, &input, &input_length) || kind != FRAME_DATA ||
        !write_all(target.stdin_write, input, input_length)) {
        goto cleanup;
    }
    HeapFree(GetProcessHeap(), 0, input);
    input = NULL;
    if (!receive_frame(pipe, &kind, &input, &input_length) || kind != FRAME_EOF) {
        goto cleanup;
    }
    if (input != NULL) HeapFree(GetProcessHeap(), 0, input);
    input = NULL;
    CloseHandle(target.stdin_write);
    target.stdin_write = NULL;
    if (!read_all(target.stdout_read, &stdout_payload, &stdout_length) ||
        !read_all(target.stderr_read, &stderr_payload, &stderr_length) ||
        WaitForSingleObject(target.process.hProcess, 30000) != WAIT_OBJECT_0 ||
        !GetExitCodeProcess(target.process.hProcess, &exit_code) ||
        !send_frame(pipe, FRAME_STDOUT, stdout_payload, stdout_length) ||
        !send_frame(pipe, FRAME_STDERR, stderr_payload, stderr_length) ||
        !send_frame(pipe, FRAME_EXIT, &exit_code, sizeof(exit_code))) {
        goto cleanup;
    }
    result = 0;

cleanup:
    if (input != NULL) HeapFree(GetProcessHeap(), 0, input);
    if (stdout_payload != NULL) HeapFree(GetProcessHeap(), 0, stdout_payload);
    if (stderr_payload != NULL) HeapFree(GetProcessHeap(), 0, stderr_payload);
    close_child(&target);
    if (pipe != INVALID_HANDLE_VALUE) CloseHandle(pipe);
    return result;
}

static int relay_mcp_mode(const wchar_t *name) {
    HANDLE pipe = connect_local_pipe(name);
    CHILD_PIPES target;
    char facts[2048];
    BYTE kind;
    BYTE *payload = NULL;
    DWORD length = 0;
    DWORD exit_code = 0;
    int index;
    int result = 110;
    ZeroMemory(&target, sizeof(target));
    if (pipe == INVALID_HANDLE_VALUE) return (int)GetLastError();
    token_facts(facts, sizeof(facts));
    if (!send_frame(pipe, FRAME_HELLO, facts, (DWORD)strlen(facts)) ||
        !spawn_target(L"mcp-target", &target) ||
        !send_target_facts(pipe, &target)) {
        goto cleanup;
    }
    for (index = 0; index < 3; index++) {
        BYTE *line = NULL;
        DWORD line_length = 0;
        BYTE *diagnostic = NULL;
        DWORD diagnostic_length = 0;
        if (!receive_frame(pipe, &kind, &payload, &length) || kind != FRAME_DATA ||
            !write_all(target.stdin_write, payload, length) ||
            !read_line(target.stdout_read, &line, &line_length) ||
            !read_line(target.stderr_read, &diagnostic, &diagnostic_length) ||
            !send_frame(pipe, FRAME_STDOUT, line, line_length) ||
            !send_frame(pipe, FRAME_STDERR, diagnostic, diagnostic_length)) {
            if (line != NULL) HeapFree(GetProcessHeap(), 0, line);
            if (diagnostic != NULL) HeapFree(GetProcessHeap(), 0, diagnostic);
            goto cleanup;
        }
        if (payload != NULL) HeapFree(GetProcessHeap(), 0, payload);
        if (line != NULL) HeapFree(GetProcessHeap(), 0, line);
        if (diagnostic != NULL) HeapFree(GetProcessHeap(), 0, diagnostic);
        payload = NULL;
    }
    if (!receive_frame(pipe, &kind, &payload, &length) || kind != FRAME_EOF) {
        goto cleanup;
    }
    if (payload != NULL) HeapFree(GetProcessHeap(), 0, payload);
    payload = NULL;
    CloseHandle(target.stdin_write);
    target.stdin_write = NULL;
    if (WaitForSingleObject(target.process.hProcess, 30000) != WAIT_OBJECT_0 ||
        !GetExitCodeProcess(target.process.hProcess, &exit_code) ||
        !send_frame(pipe, FRAME_EXIT, &exit_code, sizeof(exit_code))) {
        goto cleanup;
    }
    result = 0;

cleanup:
    if (payload != NULL) HeapFree(GetProcessHeap(), 0, payload);
    close_child(&target);
    if (pipe != INVALID_HANDLE_VALUE) CloseHandle(pipe);
    return result;
}

static int relay_cancel_mode(const wchar_t *name) {
    HANDLE pipe = connect_local_pipe(name);
    CHILD_PIPES target;
    char facts[2048];
    BYTE byte;
    DWORD received = 0;
    int result = 120;
    ZeroMemory(&target, sizeof(target));
    if (pipe == INVALID_HANDLE_VALUE) return (int)GetLastError();
    token_facts(facts, sizeof(facts));
    if (!send_frame(pipe, FRAME_HELLO, facts, (DWORD)strlen(facts)) ||
        !spawn_target(L"hold-target", &target) ||
        !send_target_facts(pipe, &target) ||
        !send_frame(pipe, FRAME_READY, NULL, 0)) {
        goto cleanup;
    }
    ReadFile(pipe, &byte, 1, &received, NULL);
    result = 0;
cleanup:
    close_child(&target);
    if (pipe != INVALID_HANDLE_VALUE) CloseHandle(pipe);
    return result;
}

static BOOL write_facts_handle(HANDLE handle) {
    char facts[2048];
    token_facts(facts, sizeof(facts));
    return write_all(handle, facts, (DWORD)strlen(facts));
}

static int stdio_target_mode(const wchar_t *facts_text) {
    HANDLE facts_handle = (HANDLE)(ULONG_PTR)_wcstoui64(facts_text, NULL, 0);
    BYTE *input = NULL;
    DWORD input_length = 0;
    static const char diagnostic[] = "TARGET_STDERR:diagnostic\r\n";
    if (!write_facts_handle(facts_handle)) return 131;
    CloseHandle(facts_handle);
    if (!read_all(GetStdHandle(STD_INPUT_HANDLE), &input, &input_length) ||
        !write_all(GetStdHandle(STD_OUTPUT_HANDLE), input, input_length) ||
        !write_all(
            GetStdHandle(STD_ERROR_HANDLE), diagnostic, (DWORD)(sizeof(diagnostic) - 1))) {
        if (input != NULL) HeapFree(GetProcessHeap(), 0, input);
        return 132;
    }
    HeapFree(GetProcessHeap(), 0, input);
    return 37;
}

static int mcp_target_mode(const wchar_t *facts_text) {
    HANDLE facts_handle = (HANDLE)(ULONG_PTR)_wcstoui64(facts_text, NULL, 0);
    int index = 0;
    if (!write_facts_handle(facts_handle)) return 141;
    CloseHandle(facts_handle);
    for (;;) {
        BYTE *request = NULL;
        DWORD request_length = 0;
        char prefix[64];
        int prefix_length;
        if (!read_line(GetStdHandle(STD_INPUT_HANDLE), &request, &request_length)) {
            DWORD error = GetLastError();
            if (error == ERROR_BROKEN_PIPE || error == ERROR_HANDLE_EOF) {
                break;
            }
            return 142;
        }
        prefix_length = _snprintf_s(prefix, sizeof(prefix), _TRUNCATE, "response-%d:", index);
        if (prefix_length < 0 ||
            !write_all(GetStdHandle(STD_OUTPUT_HANDLE), prefix, (DWORD)prefix_length) ||
            !write_all(GetStdHandle(STD_OUTPUT_HANDLE), request, request_length)) {
            HeapFree(GetProcessHeap(), 0, request);
            return 143;
        }
        prefix_length = _snprintf_s(prefix, sizeof(prefix), _TRUNCATE, "diagnostic-%d\n", index);
        if (prefix_length < 0 ||
            !write_all(GetStdHandle(STD_ERROR_HANDLE), prefix, (DWORD)prefix_length)) {
            HeapFree(GetProcessHeap(), 0, request);
            return 144;
        }
        HeapFree(GetProcessHeap(), 0, request);
        index++;
    }
    return index == 3 ? 0 : 145;
}

static int hold_target_mode(const wchar_t *facts_text) {
    HANDLE facts_handle = (HANDLE)(ULONG_PTR)_wcstoui64(facts_text, NULL, 0);
    if (!write_facts_handle(facts_handle)) return 151;
    CloseHandle(facts_handle);
    Sleep(INFINITE);
    return 0;
}

static int plain_target_mode(void) {
    BYTE *input = NULL;
    DWORD input_length = 0;
    char facts[2048];
    static const char diagnostic[] = "PLAIN_STDERR:diagnostic\n";
    token_facts(facts, sizeof(facts));
    if (!write_all(GetStdHandle(STD_ERROR_HANDLE), facts, (DWORD)strlen(facts)) ||
        !write_all(GetStdHandle(STD_ERROR_HANDLE), "\n", 1) ||
        !read_all(GetStdHandle(STD_INPUT_HANDLE), &input, &input_length) ||
        !write_all(GetStdHandle(STD_OUTPUT_HANDLE), input, input_length) ||
        !write_all(
            GetStdHandle(STD_ERROR_HANDLE), diagnostic, (DWORD)(sizeof(diagnostic) - 1))) {
        if (input != NULL) HeapFree(GetProcessHeap(), 0, input);
        return 161;
    }
    HeapFree(GetProcessHeap(), 0, input);
    return 37;
}

static int trusted_launcher_mode(const wchar_t *profile_name, const wchar_t *target_path) {
    PSID package_sid = NULL;
    SECURITY_CAPABILITIES capabilities;
    SIZE_T attribute_size = 0;
    LPPROC_THREAD_ATTRIBUTE_LIST attributes = NULL;
    STARTUPINFOEXW startup;
    PROCESS_INFORMATION process;
    wchar_t command[32768];
    DWORD exit_code = 170;
    BOOL created = FALSE;
    ZeroMemory(&capabilities, sizeof(capabilities));
    ZeroMemory(&startup, sizeof(startup));
    ZeroMemory(&process, sizeof(process));
    startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.StartupInfo.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup.StartupInfo.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    if (FAILED(DeriveAppContainerSidFromAppContainerName(profile_name, &package_sid))) {
        return 171;
    }
    capabilities.AppContainerSid = package_sid;
    InitializeProcThreadAttributeList(NULL, 1, 0, &attribute_size);
    attributes = (LPPROC_THREAD_ATTRIBUTE_LIST)HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY, attribute_size);
    if (attributes == NULL ||
        !InitializeProcThreadAttributeList(attributes, 1, 0, &attribute_size) ||
        !UpdateProcThreadAttribute(
            attributes,
            0,
            PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            &capabilities,
            sizeof(capabilities),
            NULL,
            NULL)) {
        goto cleanup;
    }
    startup.lpAttributeList = attributes;
    if (_snwprintf_s(command, 32768, _TRUNCATE, L"\"%s\" plain-target", target_path) < 0) {
        goto cleanup;
    }
    created = CreateProcessW(
        target_path,
        command,
        NULL,
        NULL,
        TRUE,
        EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
        NULL,
        NULL,
        &startup.StartupInfo,
        &process);
    if (!created) goto cleanup;
    CloseHandle(process.hThread);
    WaitForSingleObject(process.hProcess, 30000);
    GetExitCodeProcess(process.hProcess, &exit_code);
    CloseHandle(process.hProcess);

cleanup:
    if (attributes != NULL) {
        DeleteProcThreadAttributeList(attributes);
        HeapFree(GetProcessHeap(), 0, attributes);
    }
    if (package_sid != NULL) FreeSid(package_sid);
    return created ? (int)exit_code : 172;
}

static int conpty_smoke_mode(void) {
    char input[256];
    DWORD received = 0;
    printf("CONPTY_POC2B_READY\r\n");
    fflush(stdout);
    if (!ReadFile(GetStdHandle(STD_INPUT_HANDLE), input, sizeof(input), &received, NULL)) {
        return 181;
    }
    printf("CONPTY_POC2B_ECHO:");
    fwrite(input, 1, received, stdout);
    fflush(stdout);
    return 0;
}

int wmain(int argc, wchar_t **argv) {
    if (argc < 2) return 2;
    if (wcscmp(argv[1], L"pipe-denied") == 0 && argc == 3)
        return pipe_denied_mode(argv[2]);
    if (wcscmp(argv[1], L"byte-stream") == 0 && argc == 3)
        return byte_stream_mode(argv[2]);
    if (wcscmp(argv[1], L"relay-command") == 0 && argc == 4)
        return relay_command_mode(argv[2], argv[3]);
    if (wcscmp(argv[1], L"network-pipe") == 0 && argc == 5)
        return network_pipe_mode(argv[2], argv[3], argv[4]);
    if (wcscmp(argv[1], L"relay-stdio") == 0 && argc == 3)
        return relay_stdio_mode(argv[2]);
    if (wcscmp(argv[1], L"relay-mcp") == 0 && argc == 3)
        return relay_mcp_mode(argv[2]);
    if (wcscmp(argv[1], L"relay-cancel") == 0 && argc == 3)
        return relay_cancel_mode(argv[2]);
    if (wcscmp(argv[1], L"stdio-target") == 0 && argc == 3)
        return stdio_target_mode(argv[2]);
    if (wcscmp(argv[1], L"mcp-target") == 0 && argc == 3)
        return mcp_target_mode(argv[2]);
    if (wcscmp(argv[1], L"hold-target") == 0 && argc == 3)
        return hold_target_mode(argv[2]);
    if (wcscmp(argv[1], L"plain-target") == 0)
        return plain_target_mode();
    if (wcscmp(argv[1], L"trusted-launcher") == 0 && argc == 4)
        return trusted_launcher_mode(argv[2], argv[3]);
    if (wcscmp(argv[1], L"conpty-smoke") == 0)
        return conpty_smoke_mode();
    return 3;
}
