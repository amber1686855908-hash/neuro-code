#include <windows.h>
#include <userenv.h>

#pragma comment(lib, "userenv.lib")
#pragma comment(lib, "Advapi32.lib")

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

    emit_ascii("W5_GATE1_PROBE_STARTED\n");

    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        emit_ascii("W5_GATE1_TOKEN=ERROR\n");
        return 20;
    }

    if (GetUserProfileDirectoryW(token, profile, &profile_length)) {
        emit_ascii("W5_GATE1_PROFILE=AVAILABLE\n");
    } else {
        emit_ascii("W5_GATE1_PROFILE=UNAVAILABLE\n");
    }

    (void)CloseHandle(token);
    emit_ascii("W5_GATE1_PROBE_FINISHED\n");
    return 0;
}
