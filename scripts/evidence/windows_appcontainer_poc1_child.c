#define UNICODE
#define _UNICODE
#include <winsock2.h>
#include <windows.h>
#include <sddl.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
#include <ws2tcpip.h>

static void json_escape_wide(const wchar_t *input, char *output, size_t capacity) {
    int needed;
    char utf8[2048];
    size_t source = 0;
    size_t target = 0;

    needed = WideCharToMultiByte(CP_UTF8, 0, input, -1, utf8, (int)sizeof(utf8), NULL, NULL);
    if (needed <= 0) {
        utf8[0] = '\0';
    }
    while (utf8[source] != '\0' && target + 2 < capacity) {
        unsigned char value = (unsigned char)utf8[source++];
        if (value == '"' || value == '\\') {
            output[target++] = '\\';
        }
        output[target++] = (char)value;
    }
    output[target] = '\0';
}

static BOOL token_value(TOKEN_INFORMATION_CLASS kind, void **buffer, DWORD *size) {
    HANDLE token = NULL;
    DWORD needed = 0;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        return FALSE;
    }
    GetTokenInformation(token, kind, NULL, 0, &needed);
    *buffer = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, needed);
    if (*buffer == NULL) {
        CloseHandle(token);
        return FALSE;
    }
    if (!GetTokenInformation(token, kind, *buffer, needed, &needed)) {
        HeapFree(GetProcessHeap(), 0, *buffer);
        *buffer = NULL;
        CloseHandle(token);
        return FALSE;
    }
    *size = needed;
    CloseHandle(token);
    return TRUE;
}

static void facts_json(char *output, size_t capacity) {
    DWORD is_appcontainer = 0;
    DWORD returned = 0;
    HANDLE token = NULL;
    TOKEN_APPCONTAINER_INFORMATION *container = NULL;
    TOKEN_MANDATORY_LABEL *label = NULL;
    DWORD container_size = 0;
    DWORD label_size = 0;
    LPWSTR sid_text = NULL;
    wchar_t username[256];
    DWORD username_size = (DWORD)(sizeof(username) / sizeof(username[0]));
    char sid_utf8[1024];
    char user_utf8[1024];
    DWORD integrity = 0;
    BOOL in_job = FALSE;

    username[0] = L'\0';
    sid_utf8[0] = '\0';
    user_utf8[0] = '\0';
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        GetTokenInformation(
            token, TokenIsAppContainer, &is_appcontainer, sizeof(is_appcontainer), &returned);
        CloseHandle(token);
    }
    if (token_value(TokenAppContainerSid, (void **)&container, &container_size)) {
        if (container->TokenAppContainer != NULL &&
            ConvertSidToStringSidW(container->TokenAppContainer, &sid_text)) {
            json_escape_wide(sid_text, sid_utf8, sizeof(sid_utf8));
            LocalFree(sid_text);
        }
        HeapFree(GetProcessHeap(), 0, container);
    }
    if (token_value(TokenIntegrityLevel, (void **)&label, &label_size)) {
        UCHAR count = *GetSidSubAuthorityCount(label->Label.Sid);
        if (count > 0) {
            integrity = *GetSidSubAuthority(label->Label.Sid, count - 1);
        }
        HeapFree(GetProcessHeap(), 0, label);
    }
    GetUserNameW(username, &username_size);
    json_escape_wide(username, user_utf8, sizeof(user_utf8));
    IsProcessInJob(GetCurrentProcess(), NULL, &in_job);
    _snprintf_s(
        output,
        capacity,
        _TRUNCATE,
        "{\"pid\":%lu,\"token_is_appcontainer\":%s,\"appcontainer_sid\":\"%s\","
        "\"integrity_rid\":%lu,\"username\":\"%s\",\"in_job\":%s}",
        GetCurrentProcessId(),
        is_appcontainer ? "true" : "false",
        sid_utf8,
        integrity,
        user_utf8,
        in_job ? "true" : "false");
}

static BOOL append_line(const wchar_t *path, const char *line) {
    HANDLE file = CreateFileW(
        path,
        FILE_APPEND_DATA,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        NULL);
    DWORD written = 0;
    BOOL ok;
    if (file == INVALID_HANDLE_VALUE) {
        return FALSE;
    }
    ok = WriteFile(file, line, (DWORD)strlen(line), &written, NULL);
    if (ok) {
        const char newline = '\n';
        ok = WriteFile(file, &newline, 1, &written, NULL);
    }
    CloseHandle(file);
    return ok;
}

static BOOL spawn_self(const wchar_t *arguments, PROCESS_INFORMATION *process) {
    wchar_t executable[MAX_PATH];
    wchar_t command[32768];
    STARTUPINFOW startup;
    if (!GetModuleFileNameW(NULL, executable, MAX_PATH)) {
        return FALSE;
    }
    if (_snwprintf_s(command, 32768, _TRUNCATE, L"\"%s\" %s", executable, arguments) < 0) {
        return FALSE;
    }
    ZeroMemory(&startup, sizeof(startup));
    ZeroMemory(process, sizeof(*process));
    startup.cb = sizeof(startup);
    return CreateProcessW(
        executable,
        command,
        NULL,
        NULL,
        FALSE,
        CREATE_NO_WINDOW,
        NULL,
        NULL,
        &startup,
        process);
}

static int record_facts(const wchar_t *path) {
    char facts[4096];
    facts_json(facts, sizeof(facts));
    return append_line(path, facts) ? 0 : 31;
}

static int tree_mode(const wchar_t *path, int depth) {
    char facts[4096];
    PROCESS_INFORMATION process;
    wchar_t arguments[32768];
    facts_json(facts, sizeof(facts));
    if (!append_line(path, facts)) {
        return 32;
    }
    if (depth < 2) {
        _snwprintf_s(
            arguments, 32768, _TRUNCATE, L"tree \"%s\" %d", path, depth + 1);
        if (!spawn_self(arguments, &process)) {
            return 33;
        }
        CloseHandle(process.hThread);
        WaitForSingleObject(process.hProcess, INFINITE);
        CloseHandle(process.hProcess);
    } else {
        Sleep(300000);
    }
    return 0;
}

static int stdio_mode(const wchar_t *sentinel_text) {
    char input[512];
    DWORD read = 0;
    DWORD written = 0;
    HANDLE sentinel = (HANDLE)(ULONG_PTR)_wcstoui64(sentinel_text, NULL, 0);
    DWORD flags = 0;
    BOOL visible = GetHandleInformation(sentinel, &flags);
    DWORD sentinel_error = visible ? 0 : GetLastError();
    if (!ReadFile(GetStdHandle(STD_INPUT_HANDLE), input, sizeof(input) - 1, &read, NULL)) {
        return 41;
    }
    input[read] = '\0';
    WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), "STDOUT:", 7, &written, NULL);
    WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), input, read, &written, NULL);
    WriteFile(GetStdHandle(STD_ERROR_HANDLE), "STDERR:ok\n", 10, &written, NULL);
    printf("SENTINEL_VISIBLE:%s ERROR:%lu\n", visible ? "true" : "false", sentinel_error);
    fflush(stdout);
    return visible ? 42 : 0;
}

static int conpty_mode(const wchar_t *report) {
    char facts[4096];
    PROCESS_INFORMATION descendant;
    wchar_t arguments[32768];
    char input[512];
    DWORD read = 0;
    facts_json(facts, sizeof(facts));
    if (!append_line(report, facts)) {
        return 51;
    }
    _snwprintf_s(arguments, 32768, _TRUNCATE, L"record \"%s\"", report);
    if (!spawn_self(arguments, &descendant)) {
        return 52;
    }
    CloseHandle(descendant.hThread);
    WaitForSingleObject(descendant.hProcess, INFINITE);
    CloseHandle(descendant.hProcess);
    printf("CONPTY_READY\r\n");
    fflush(stdout);
    if (!ReadFile(GetStdHandle(STD_INPUT_HANDLE), input, sizeof(input) - 1, &read, NULL)) {
        return 53;
    }
    input[read] = '\0';
    printf("CONPTY_ECHO:%s", input);
    fflush(stdout);
    return 0;
}

static int network_mode(const wchar_t *host_wide, const wchar_t *port_wide, const wchar_t *report) {
    WSADATA data;
    ADDRINFOW hints;
    PADDRINFOW addresses = NULL;
    PADDRINFOW current;
    SOCKET socket_handle = INVALID_SOCKET;
    int dns_result;
    int connect_result = SOCKET_ERROR;
    int connect_error = 0;
    char result[1024];
    if (WSAStartup(MAKEWORD(2, 2), &data) != 0) {
        return 61;
    }
    ZeroMemory(&hints, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    dns_result = GetAddrInfoW(host_wide, port_wide, &hints, &addresses);
    if (dns_result == 0) {
        for (current = addresses; current != NULL; current = current->ai_next) {
            socket_handle = socket(current->ai_family, current->ai_socktype, current->ai_protocol);
            if (socket_handle == INVALID_SOCKET) {
                continue;
            }
            connect_result = connect(socket_handle, current->ai_addr, (int)current->ai_addrlen);
            if (connect_result == 0) {
                break;
            }
            connect_error = WSAGetLastError();
            closesocket(socket_handle);
            socket_handle = INVALID_SOCKET;
        }
        FreeAddrInfoW(addresses);
    } else {
        connect_error = dns_result;
    }
    _snprintf_s(
        result,
        sizeof(result),
        _TRUNCATE,
        "{\"dns\":%s,\"connected\":%s,\"error\":%d}",
        dns_result == 0 ? "true" : "false",
        connect_result == 0 ? "true" : "false",
        connect_error);
    if (socket_handle != INVALID_SOCKET) {
        closesocket(socket_handle);
    }
    WSACleanup();
    return append_line(report, result) ? 0 : 62;
}

static BOOL can_read(const wchar_t *path) {
    HANDLE handle = CreateFileW(
        path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (handle == INVALID_HANDLE_VALUE) {
        return FALSE;
    }
    CloseHandle(handle);
    return TRUE;
}

static BOOL can_write(const wchar_t *path) {
    HANDLE handle = CreateFileW(
        path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (handle == INVALID_HANDLE_VALUE) {
        return FALSE;
    }
    CloseHandle(handle);
    return TRUE;
}

static int filesystem_mode(
    const wchar_t *authorized,
    const wchar_t *outside,
    const wchar_t *write_path,
    const wchar_t *report) {
    BOOL authorized_read = can_read(authorized);
    DWORD authorized_error = authorized_read ? 0 : GetLastError();
    BOOL outside_read = can_read(outside);
    DWORD outside_error = outside_read ? 0 : GetLastError();
    BOOL authorized_write = can_write(write_path);
    DWORD write_error = authorized_write ? 0 : GetLastError();
    char result[1024];
    _snprintf_s(
        result,
        sizeof(result),
        _TRUNCATE,
        "{\"authorized_read\":%s,\"authorized_error\":%lu,"
        "\"outside_read\":%s,\"outside_error\":%lu,"
        "\"authorized_write\":%s,\"write_error\":%lu}",
        authorized_read ? "true" : "false",
        authorized_error,
        outside_read ? "true" : "false",
        outside_error,
        authorized_write ? "true" : "false",
        write_error);
    return append_line(report, result) ? 0 : 71;
}

int wmain(int argc, wchar_t **argv) {
    char facts[4096];
    if (argc < 2) {
        return 2;
    }
    if (wcscmp(argv[1], L"facts") == 0) {
        facts_json(facts, sizeof(facts));
        puts(facts);
        return 0;
    }
    if (wcscmp(argv[1], L"record") == 0 && argc == 3) {
        return record_facts(argv[2]);
    }
    if (wcscmp(argv[1], L"tree") == 0 && argc == 4) {
        return tree_mode(argv[2], _wtoi(argv[3]));
    }
    if (wcscmp(argv[1], L"stdio") == 0 && argc == 3) {
        return stdio_mode(argv[2]);
    }
    if (wcscmp(argv[1], L"conpty") == 0 && argc == 3) {
        return conpty_mode(argv[2]);
    }
    if (wcscmp(argv[1], L"network") == 0 && argc == 5) {
        return network_mode(argv[2], argv[3], argv[4]);
    }
    if (wcscmp(argv[1], L"filesystem") == 0 && argc == 6) {
        return filesystem_mode(argv[2], argv[3], argv[4], argv[5]);
    }
    return 3;
}
