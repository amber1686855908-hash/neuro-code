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

static BOOL equals_ignore_case(const wchar_t *left, const wchar_t *right) {
    return _wcsicmp(left, right) == 0;
}

int wmain(int argc, wchar_t **argv) {
    const wchar_t *target;
    HMODULE before;
    HMODULE loaded;
    BOOL freed;

    emit_ascii("W5_GATE115_LOADER_STARTED\n");
    if (argc != 2) {
        emit_ascii("W5_GATE115_LOADER_ARGUMENTS=FAIL\n");
        emit_ascii("W5_GATE115_LOADER_FINISHED\n");
        return 20;
    }
    target = argv[1];
    if (!equals_ignore_case(target, L"sechost.dll") &&
        !equals_ignore_case(target, L"bcryptprimitives.dll") &&
        !equals_ignore_case(target, L"bcrypt.dll")) {
        emit_ascii("W5_GATE115_LOADER_ARGUMENTS=FAIL\n");
        emit_ascii("W5_GATE115_LOADER_FINISHED\n");
        return 21;
    }

    before = GetModuleHandleW(target);
    emit_ascii("W5_GATE115_LOADER_PRELOADED=");
    emit_ascii(before == NULL ? "NO\n" : "YES\n");
    if (before != NULL) {
        emit_ascii("W5_GATE115_LOADER_PRELOADED_INVALID=YES\n");
        emit_ascii("W5_GATE115_LOADER_FINISHED\n");
        return 22;
    }
    emit_ascii("W5_GATE115_LOADER_PRELOADED_INVALID=NO\n");

    SetLastError(ERROR_SUCCESS);
    loaded = LoadLibraryW(target);
    if (loaded == NULL) {
        emit_ascii("W5_GATE115_LOADER_LOAD=FAIL\n");
        emit_u32("W5_GATE115_LOADER_LOAD_ERROR=", GetLastError());
        emit_ascii("W5_GATE115_LOADER_HANDLE=ZERO\n");
        emit_ascii("W5_GATE115_LOADER_FINISHED\n");
        return 24;
    }
    emit_ascii("W5_GATE115_LOADER_LOAD=PASS\n");
    emit_ascii("W5_GATE115_LOADER_HANDLE=NONZERO\n");
    freed = FreeLibrary(loaded);
    emit_ascii("W5_GATE115_LOADER_FREE=");
    emit_ascii(freed ? "PASS\n" : "FAIL\n");
    emit_ascii("W5_GATE115_LOADER_FINISHED\n");
    return freed ? 0 : 25;
}
