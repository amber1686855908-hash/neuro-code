/*
 * Acceptance-only native filesystem probe for the W4 PTY gate.
 *
 * The binary is compiled by trusted MSVC on the Windows acceptance runner and
 * copied into a disposable workspace.  It never receives credentials or
 * setup state.  Each invocation performs exactly one bounded filesystem
 * operation and reports only ALLOW/DENY on the ConPTY output stream.
 */

#define WIN32_LEAN_AND_MEAN

#include <windows.h>

#include <string.h>

static HANDLE output_handle(void) {
    return GetStdHandle(STD_OUTPUT_HANDLE);
}

static int write_bytes(const char *value, DWORD length) {
    DWORD written = 0;
    HANDLE output = output_handle();
    if (output == NULL || output == INVALID_HANDLE_VALUE) {
        return 0;
    }
    if (!WriteFile(output, value, length, &written, NULL)) {
        return 0;
    }
    return written == length;
}

static int write_text(const char *value) {
    return write_bytes(value, (DWORD)strlen(value));
}

static int emit_result(int allowed) {
    return write_text(allowed ? "W4_SEC=ALLOW\n" : "W4_SEC=DENY\n");
}

static int write_target(const char *path, int append) {
    DWORD disposition = append ? OPEN_ALWAYS : CREATE_ALWAYS;
    DWORD access = append ? FILE_APPEND_DATA : GENERIC_WRITE;
    HANDLE handle = CreateFileA(
        path,
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL,
        disposition,
        FILE_ATTRIBUTE_NORMAL,
        NULL
    );
    if (handle == INVALID_HANDLE_VALUE) {
        return 0;
    }
    static const char marker[] = "W4_PTY_WRITE_SENTINEL\r\n";
    DWORD written = 0;
    BOOL ok = WriteFile(handle, marker, (DWORD)(sizeof(marker) - 1u), &written, NULL);
    CloseHandle(handle);
    return ok && written == (DWORD)(sizeof(marker) - 1u);
}

static int read_target(const char *path) {
    HANDLE handle = CreateFileA(
        path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        NULL
    );
    if (handle == INVALID_HANDLE_VALUE) {
        return 0;
    }
    char buffer[128];
    DWORD received = 0;
    BOOL ok = ReadFile(handle, buffer, (DWORD)sizeof(buffer), &received, NULL);
    CloseHandle(handle);
    return ok && received > 0;
}

static int rename_target(const char *source, const char *destination) {
    return MoveFileExA(source, destination, MOVEFILE_REPLACE_EXISTING) != 0;
}

static int delete_target(const char *path) {
    return DeleteFileA(path) != 0;
}

int main(int argc, char **argv) {
    if (argc < 3) {
        return 90;
    }

    int allowed = 0;
    if (strcmp(argv[1], "write") == 0) {
        allowed = write_target(argv[2], 0);
    } else if (strcmp(argv[1], "append") == 0) {
        allowed = write_target(argv[2], 1);
    } else if (strcmp(argv[1], "read") == 0) {
        allowed = read_target(argv[2]);
    } else if (strcmp(argv[1], "rename") == 0 && argc >= 4) {
        allowed = rename_target(argv[2], argv[3]);
    } else if (strcmp(argv[1], "delete") == 0) {
        allowed = delete_target(argv[2]);
    } else {
        return 91;
    }

    (void)emit_result(allowed);
    return allowed ? 0 : 1;
}
