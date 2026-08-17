#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

#include <windows.h>
#include <stdio.h>

static void emit_ascii(const char *text) {
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output != NULL && output != INVALID_HANDLE_VALUE) {
        (void)WriteFile(output, text, (DWORD)lstrlenA(text), &written, NULL);
    }
}

static void emit_u32(const char *prefix, DWORD value) {
    char line[128];
    (void)snprintf(line, sizeof(line), "%s%lu\n", prefix, (unsigned long)value);
    emit_ascii(line);
}

int main(void) {
    HANDLE standard_input;
    HMODULE preloaded;
    HMODULE module;
    DWORD error;
    DWORD read = 0;
    char release_byte = 0;

    emit_ascii("W5_GATE116_STARTED=OBSERVED\n");
    emit_u32("W5_GATE116_PID=", GetCurrentProcessId());

    preloaded = GetModuleHandleW(L"bcrypt.dll");
    if (preloaded != NULL) {
        emit_ascii("W5_GATE116_PRELOADED=YES\n");
        emit_ascii("W5_GATE116_PRELOADED_INVALID=YES\n");
        return 26;
    }
    emit_ascii("W5_GATE116_PRELOADED=NO\n");

    emit_ascii("W5_GATE116_READY=OBSERVED\n");
    standard_input = GetStdHandle(STD_INPUT_HANDLE);
    if (standard_input == NULL || standard_input == INVALID_HANDLE_VALUE ||
        !ReadFile(standard_input, &release_byte, 1, &read, NULL) || read != 1) {
        error = GetLastError();
        emit_ascii("W5_GATE116_RELEASE=FAIL\n");
        emit_u32("W5_GATE116_RELEASE_ERROR=", error);
        return 27;
    }
    emit_ascii("W5_GATE116_RELEASE=PASS\n");

    SetLastError(ERROR_SUCCESS);
    module = LoadLibraryW(L"bcrypt.dll");
    if (module == NULL) {
        error = GetLastError();
        emit_ascii("W5_GATE116_LOAD=FAIL\n");
        emit_u32("W5_GATE116_LOAD_ERROR=", error);
        emit_ascii("W5_GATE116_HANDLE=ZERO\n");
        emit_ascii("W5_GATE116_FINISHED=OBSERVED\n");
        return 24;
    }
    emit_ascii("W5_GATE116_LOAD=PASS\n");
    emit_ascii("W5_GATE116_HANDLE=NONZERO\n");
    emit_ascii(FreeLibrary(module) ? "W5_GATE116_FREE=PASS\n" : "W5_GATE116_FREE=FAIL\n");
    emit_ascii("W5_GATE116_FINISHED=OBSERVED\n");
    return 0;
}
