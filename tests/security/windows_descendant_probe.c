/*
 * Acceptance-only Win32 probe for W3 Gate 5A.
 *
 * The leader is the final CreateProcessAsUserW child.  It creates this same
 * executable as a grandchild with bInheritHandles=FALSE and with no standard
 * handles in STARTUPINFO.  The grandchild therefore proves that its lifetime
 * is not coupled to the controller's stdout/stderr/stdin relay pipes.  Windows
 * may still expose default non-pipe standard handles in a child; only an
 * inherited FILE_TYPE_PIPE handle can be one of the runner's relay handles.
 * It remains in the inherited Windows Job Object for a bounded interval,
 * writes fixed workspace markers, and exits naturally.
 */

#define UNICODE
#define _UNICODE
#include <windows.h>
#include <wchar.h>

static int append_wide(wchar_t *buffer, size_t capacity, size_t *length,
                       const wchar_t *value) {
    size_t value_length = wcslen(value);
    if (*length + value_length + 1 > capacity) {
        return 0;
    }
    CopyMemory(buffer + *length, value, value_length * sizeof(wchar_t));
    *length += value_length;
    buffer[*length] = L'\0';
    return 1;
}

static int join_path(const wchar_t *root, const wchar_t *name,
                     wchar_t *path, size_t capacity) {
    size_t root_length = wcslen(root);
    size_t name_length = wcslen(name);
    size_t separator = root_length > 0 &&
                               (root[root_length - 1] == L'\\' ||
                                root[root_length - 1] == L'/')
                           ? 0
                           : 1;
    if (root_length + separator + name_length + 1 > capacity) {
        return 0;
    }
    CopyMemory(path, root, root_length * sizeof(wchar_t));
    if (separator != 0) {
        path[root_length] = L'\\';
    }
    CopyMemory(path + root_length + separator, name,
               name_length * sizeof(wchar_t));
    path[root_length + separator + name_length] = L'\0';
    return 1;
}

static int write_bytes(const wchar_t *path, const char *bytes, DWORD length) {
    HANDLE file = CreateFileW(path, GENERIC_WRITE,
                               FILE_SHARE_READ | FILE_SHARE_WRITE |
                                   FILE_SHARE_DELETE,
                               NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL,
                               NULL);
    DWORD written = 0;
    BOOL ok;
    if (file == INVALID_HANDLE_VALUE) {
        return 0;
    }
    ok = WriteFile(file, bytes, length, &written, NULL);
    CloseHandle(file);
    return ok && written == length;
}

static int write_marker(const wchar_t *root, const wchar_t *name,
                        const char *content) {
    wchar_t path[32768];
    size_t length = 0;
    while (content[length] != '\0') {
        ++length;
    }
    return join_path(root, name, path, sizeof(path) / sizeof(path[0])) &&
           write_bytes(path, content, (DWORD)length);
}

static int write_pid(const wchar_t *root) {
    char digits[32];
    DWORD pid = GetCurrentProcessId();
    size_t cursor = sizeof(digits) - 1;
    wchar_t path[32768];
    digits[cursor] = '\0';
    do {
        digits[--cursor] = (char)('0' + (pid % 10));
        pid /= 10;
    } while (pid != 0 && cursor > 0);
    if (!join_path(root, L"grandchild.pid", path,
                   sizeof(path) / sizeof(path[0]))) {
        return 0;
    }
    return write_bytes(path, digits + cursor,
                       (DWORD)(sizeof(digits) - 1 - cursor));
}

static int standard_pipe_handle_is_valid(DWORD which) {
    HANDLE handle = GetStdHandle(which);
    if (handle == NULL || handle == INVALID_HANDLE_VALUE) {
        return 0;
    }
    return GetFileType(handle) == FILE_TYPE_PIPE;
}

static int marker_exists(const wchar_t *root, const wchar_t *name) {
    wchar_t path[32768];
    if (!join_path(root, name, path, sizeof(path) / sizeof(path[0]))) {
        return 0;
    }
    return GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}

static int wait_for_markers(const wchar_t *root) {
    DWORD elapsed = 0;
    while (elapsed < 5000) {
        if (marker_exists(root, L"grandchild-started") &&
            marker_exists(root, L"grandchild.pid") &&
            (marker_exists(root, L"grandchild-stdio-free") ||
             marker_exists(root, L"grandchild-stdio-inherited"))) {
            return 1;
        }
        Sleep(20);
        elapsed += 20;
    }
    return 0;
}

static int run_grandchild(const wchar_t *root) {
    int has_runner_pipes =
        standard_pipe_handle_is_valid(STD_INPUT_HANDLE) ||
        standard_pipe_handle_is_valid(STD_OUTPUT_HANDLE) ||
        standard_pipe_handle_is_valid(STD_ERROR_HANDLE);
    if (!write_marker(root, L"grandchild-started", "grandchild-started\n") ||
        !write_pid(root)) {
        return 31;
    }
    if (has_runner_pipes) {
        if (!write_marker(root, L"grandchild-stdio-inherited",
                          "grandchild-stdio-inherited\n")) {
            return 32;
        }
    } else if (!write_marker(root, L"grandchild-stdio-free",
                             "grandchild-stdio-free\n")) {
        return 33;
    }
    Sleep(2500);
    if (!write_marker(root, L"grandchild-finished", "grandchild-finished\n")) {
        return 34;
    }
    return 0;
}

static int run_leader(const wchar_t *root) {
    wchar_t module[32768];
    wchar_t command_line[32768];
    size_t command_length = 0;
    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    DWORD module_length;
    const DWORD module_capacity = (DWORD)(sizeof(module) / sizeof(module[0]));
    int started;

    module_length = GetModuleFileNameW(NULL, module, module_capacity);
    if (module_length == 0 || module_length >= module_capacity) {
        return 41;
    }
    ZeroMemory(command_line, sizeof(command_line));
    if (!append_wide(command_line, sizeof(command_line) / sizeof(command_line[0]),
                     &command_length, L"\"") ||
        !append_wide(command_line, sizeof(command_line) / sizeof(command_line[0]),
                     &command_length, module) ||
        !append_wide(command_line, sizeof(command_line) / sizeof(command_line[0]),
                     &command_length, L"\" grandchild \"") ||
        !append_wide(command_line, sizeof(command_line) / sizeof(command_line[0]),
                     &command_length, root) ||
        !append_wide(command_line, sizeof(command_line) / sizeof(command_line[0]),
                     &command_length, L"\"")) {
        return 42;
    }
    ZeroMemory(&startup, sizeof(startup));
    startup.cb = (DWORD)sizeof(startup);
    ZeroMemory(&process, sizeof(process));
    /* No inherited handles and no STARTF_USESTDHANDLES: stdio is detached. */
    started = CreateProcessW(NULL, command_line, NULL, NULL, FALSE,
                             CREATE_NO_WINDOW, NULL, NULL, &startup, &process);
    if (!started) {
        return 43;
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    if (!wait_for_markers(root)) {
        return 44;
    }
    if (!write_marker(root, L"leader-exiting", "leader-exiting\n")) {
        return 45;
    }
    return 23;
}

int wmain(int argc, wchar_t **argv) {
    if (argc == 3 && wcscmp(argv[1], L"grandchild") == 0) {
        return run_grandchild(argv[2]);
    }
    if (argc == 3 && wcscmp(argv[1], L"parent-exit-child-holds") == 0) {
        return run_leader(argv[2]);
    }
    return 64;
}
