#include <windows.h>
#include <stdio.h>

int wmain(int argc, wchar_t **argv) {
    HANDLE file;
    char buffer[64];
    DWORD read = 0;
    if (argc != 2) {
        fputs("W5_GATE120_READ=INVALID\n", stdout);
        return 90;
    }
    file = CreateFileW(
        argv[1],
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        NULL
    );
    if (file == INVALID_HANDLE_VALUE) {
        printf("W5_GATE120_READ=DENY\nW5_GATE120_READ_ERROR=%lu\n", (unsigned long)GetLastError());
        return 5;
    }
    if (!ReadFile(file, buffer, (DWORD)sizeof(buffer), &read, NULL)) {
        printf("W5_GATE120_READ=DENY\nW5_GATE120_READ_ERROR=%lu\n", (unsigned long)GetLastError());
        CloseHandle(file);
        return 5;
    }
    CloseHandle(file);
    fputs("W5_GATE120_READ=PASS\n", stdout);
    return 0;
}
