#include <windows.h>
#include <stdio.h>

int wmain(int argc, wchar_t **argv) {
    HANDLE file;
    const char payload[] = "W5_GATE110_WRITE\n";
    DWORD written = 0;
    if (argc != 2) {
        fputs("W5_GATE110_WRITE=INVALID\n", stdout);
        return 90;
    }
    file = CreateFileW(
        argv[1],
        GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        NULL
    );
    if (file == INVALID_HANDLE_VALUE) {
        printf("W5_GATE110_WRITE=DENY\nW5_GATE110_WRITE_ERROR=%lu\n", (unsigned long)GetLastError());
        return 5;
    }
    if (!WriteFile(file, payload, (DWORD)(sizeof(payload) - 1), &written, NULL) ||
        written != (DWORD)(sizeof(payload) - 1)) {
        printf("W5_GATE110_WRITE=DENY\nW5_GATE110_WRITE_ERROR=%lu\n", (unsigned long)GetLastError());
        (void)CloseHandle(file);
        return 5;
    }
    (void)CloseHandle(file);
    fputs("W5_GATE110_WRITE=PASS\n", stdout);
    return 0;
}
