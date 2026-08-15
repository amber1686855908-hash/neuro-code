#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

#include <windows.h>
#include <ncrypt.h>
#include <stdio.h>
#include <userenv.h>

static void emit_ascii(const char *text) {
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output != NULL && output != INVALID_HANDLE_VALUE) {
        (void)WriteFile(output, text, (DWORD)lstrlenA(text), &written, NULL);
    }
}

static void emit_status(const char *prefix, SECURITY_STATUS status) {
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
    NCRYPT_PROV_HANDLE provider = 0;
    SECURITY_STATUS status;

    emit_ascii("W5_GATE16_P2_STARTED\n");
    status = NCryptOpenStorageProvider(&provider, MS_KEY_STORAGE_PROVIDER, 0);
    emit_status("W5_GATE16_P2_NCRYPT_STATUS=", status);
    if (status == ERROR_SUCCESS && provider != 0) {
        (void)NCryptFreeObject(provider);
    }
    touch_baseline_imports();
    emit_ascii("W5_GATE16_P2_FINISHED\n");
    return status == ERROR_SUCCESS ? 0 : 22;
}
