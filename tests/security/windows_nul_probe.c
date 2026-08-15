#define WIN32_LEAN_AND_MEAN

#include <windows.h>

#include <stdio.h>

static void emit_result(const char *create_status, DWORD create_error,
                        const char *write_status, DWORD write_error) {
    printf("W5_NUL_DIRECT={\"create_file\":\"%s\",\"create_error\":%lu,"
           "\"write\":\"%s\",\"write_error\":%lu}\n",
           create_status, (unsigned long)create_error, write_status,
           (unsigned long)write_error);
}

int main(void) {
    HANDLE nul = CreateFileW(L"NUL", GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                             NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (nul == INVALID_HANDLE_VALUE) {
        DWORD error = GetLastError();
        emit_result("FAIL", error, "NOT_ATTEMPTED", 0);
        return 2;
    }

    static const char marker[] = "W5_NUL_DIRECT_OK\n";
    DWORD written = 0;
    BOOL wrote = WriteFile(nul, marker, (DWORD)(sizeof(marker) - 1U), &written, NULL);
    DWORD write_error = wrote ? 0 : GetLastError();
    CloseHandle(nul);
    emit_result("PASS", 0, wrote && written == (DWORD)(sizeof(marker) - 1U) ? "PASS" : "FAIL",
                write_error);
    return wrote && written == (DWORD)(sizeof(marker) - 1U) ? 0 : 3;
}
