#define WIN32_LEAN_AND_MEAN

#include <windows.h>

#include <stdio.h>

typedef struct NulProbeResult {
    const char *create_status;
    DWORD create_error;
    const char *write_status;
    DWORD write_error;
} NulProbeResult;

static NulProbeResult probe_nul(DWORD access, BOOL write_after_open) {
    HANDLE nul = CreateFileW(L"NUL", access, FILE_SHARE_READ | FILE_SHARE_WRITE,
                             NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (nul == INVALID_HANDLE_VALUE) {
        NulProbeResult result = {"FAIL", GetLastError(), "NOT_ATTEMPTED", 0};
        return result;
    }

    if (!write_after_open) {
        NulProbeResult result = {"PASS", 0, "NOT_ATTEMPTED", 0};
        CloseHandle(nul);
        return result;
    }

    static const char marker[] = "W5_NUL_DIRECT_OK\n";
    DWORD written = 0;
    BOOL wrote = WriteFile(nul, marker, (DWORD)(sizeof(marker) - 1U), &written, NULL);
    DWORD write_error = wrote ? 0 : GetLastError();
    NulProbeResult result = {
        "PASS",
        0,
        wrote && written == (DWORD)(sizeof(marker) - 1U) ? "PASS" : "FAIL",
        write_error,
    };
    CloseHandle(nul);
    return result;
}

static void emit_result(const char *name, NulProbeResult result) {
    printf("\"%s\":{"
           "\"create\":\"%s\",\"create_error\":%lu,"
           "\"write\":\"%s\",\"write_error\":%lu},",
           name, result.create_status, (unsigned long)result.create_error,
           result.write_status, (unsigned long)result.write_error);
}

int main(void) {
    NulProbeResult read = probe_nul(GENERIC_READ, FALSE);
    NulProbeResult write = probe_nul(GENERIC_WRITE, TRUE);
    NulProbeResult read_write = probe_nul(GENERIC_READ | GENERIC_WRITE, TRUE);

    printf("W5_NUL_DIRECT={");
    emit_result("read", read);
    emit_result("write", write);
    printf("\"read_write\":{"
           "\"create\":\"%s\",\"create_error\":%lu,"
           "\"write\":\"%s\",\"write_error\":%lu}}\n",
           read_write.create_status, (unsigned long)read_write.create_error,
           read_write.write_status, (unsigned long)read_write.write_error);

    return (read.create_status[0] == 'P' && write.create_status[0] == 'P' &&
            read_write.create_status[0] == 'P' && write.write_status[0] == 'P' &&
            read_write.write_status[0] == 'P')
               ? 0
               : 2;
}
