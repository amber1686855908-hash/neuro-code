#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

#include <windows.h>
#include <userenv.h>

static void emit_ascii(const char *text) {
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output != NULL && output != INVALID_HANDLE_VALUE) {
        (void)WriteFile(output, text, (DWORD)lstrlenA(text), &written, NULL);
    }
}

int main(void) {
    HANDLE token = NULL;
    wchar_t profile[32768];
    DWORD profile_length = (DWORD)(sizeof(profile) / sizeof(profile[0]));

    emit_ascii("W5_GATE16_P0_STARTED\n");
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        emit_ascii("W5_GATE16_P0_TOKEN=ERROR\n");
        emit_ascii("W5_GATE16_P0_FINISHED\n");
        return 20;
    }
    (void)GetUserProfileDirectoryW(token, profile, &profile_length);
    (void)CloseHandle(token);
    emit_ascii("W5_GATE16_P0_FINISHED\n");
    return 0;
}
