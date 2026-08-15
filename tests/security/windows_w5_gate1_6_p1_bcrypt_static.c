#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

#include <windows.h>
#include <bcrypt.h>
#include <stdio.h>
#include <userenv.h>

static void emit_ascii(const char *text) {
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output != NULL && output != INVALID_HANDLE_VALUE) {
        (void)WriteFile(output, text, (DWORD)lstrlenA(text), &written, NULL);
    }
}

static void emit_status(const char *prefix, NTSTATUS status) {
    char line[128];
    (void)snprintf(line, sizeof(line), "%s0x%08lX\n", prefix, (unsigned long)status);
    emit_ascii(line);
}

static void touch_baseline_imports(void) {
    HANDLE token = NULL;
    wchar_t profile[32768];
    DWORD profile_length = (DWORD)(sizeof(profile) / sizeof(profile[0]));
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        (void)GetUserProfileDirectoryW(token, profile, &profile_length);
        (void)CloseHandle(token);
    }
}

int main(void) {
    unsigned char buffer[32] = {0};
    NTSTATUS status;

    emit_ascii("W5_GATE16_P1_STARTED\n");
    status = BCryptGenRandom(
        NULL,
        buffer,
        (ULONG)sizeof(buffer),
        BCRYPT_USE_SYSTEM_PREFERRED_RNG
    );
    emit_status("W5_GATE16_P1_BCRYPT_STATUS=", status);
    touch_baseline_imports();
    emit_ascii("W5_GATE16_P1_FINISHED\n");
    return status == 0 ? 0 : 21;
}
