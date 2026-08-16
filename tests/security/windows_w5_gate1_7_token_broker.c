#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

#include <windows.h>
#include <sddl.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#define TOKEN_USER_INFORMATION 1
#define TOKEN_RESTRICTED_SIDS_INFORMATION 11
#define TOKEN_DUPLICATE_ACCESS 0x0002
#define TOKEN_QUERY_ACCESS 0x0008
#define TOKEN_ASSIGN_PRIMARY_ACCESS 0x0001
#define TOKEN_ADJUST_DEFAULT_ACCESS 0x0080
#define DISABLE_MAX_PRIVILEGE_FLAG 0x00000001
#define LUA_TOKEN_FLAG 0x00000004
#define WRITE_RESTRICTED_FLAG 0x00000008
#define CREATE_UNICODE_ENVIRONMENT_FLAG 0x00000400
#define CREATE_NO_WINDOW_FLAG 0x08000000
#define STARTF_USESTDHANDLES_FLAG 0x00000100
#define WAIT_OBJECT_0_RESULT 0x00000000
#define WAIT_TIMEOUT_RESULT 0x00000102

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

static void emit_bool(const char *prefix, BOOL value) {
    emit_ascii(prefix);
    emit_ascii(value ? "PASS\n" : "FAIL\n");
}

static BOOL variant_flags(const wchar_t *variant, DWORD *flags, BOOL *has_sid) {
    *flags = 0;
    *has_sid = FALSE;
    if (wcscmp(variant, L"U") == 0) {
        return TRUE;
    }
    if (wcscmp(variant, L"D") == 0) {
        *flags = DISABLE_MAX_PRIVILEGE_FLAG;
    } else if (wcscmp(variant, L"L") == 0) {
        *flags = LUA_TOKEN_FLAG;
    } else if (wcscmp(variant, L"DL") == 0) {
        *flags = DISABLE_MAX_PRIVILEGE_FLAG | LUA_TOKEN_FLAG;
    } else if (wcscmp(variant, L"W") == 0) {
        *flags = WRITE_RESTRICTED_FLAG;
        *has_sid = TRUE;
    } else if (wcscmp(variant, L"DW") == 0) {
        *flags = DISABLE_MAX_PRIVILEGE_FLAG | WRITE_RESTRICTED_FLAG;
        *has_sid = TRUE;
    } else if (wcscmp(variant, L"LW") == 0) {
        *flags = LUA_TOKEN_FLAG | WRITE_RESTRICTED_FLAG;
        *has_sid = TRUE;
    } else if (wcscmp(variant, L"DLW") == 0) {
        *flags = DISABLE_MAX_PRIVILEGE_FLAG | LUA_TOKEN_FLAG | WRITE_RESTRICTED_FLAG;
        *has_sid = TRUE;
    } else {
        return FALSE;
    }
    return TRUE;
}

static BOOL inspect_restricted_sids(HANDLE token, PSID expected_sid, DWORD expected_count) {
    DWORD required = 0;
    TOKEN_GROUPS *groups = NULL;
    BOOL matched = expected_count == 0;
    if (GetTokenInformation(token, TOKEN_RESTRICTED_SIDS_INFORMATION, NULL, 0, &required) ||
        GetLastError() != ERROR_INSUFFICIENT_BUFFER || required < sizeof(DWORD)) {
        return FALSE;
    }
    groups = (TOKEN_GROUPS *)malloc(required);
    if (groups == NULL) {
        return FALSE;
    }
    if (!GetTokenInformation(
        token,
        TOKEN_RESTRICTED_SIDS_INFORMATION,
        groups,
        required,
        &required
    )) {
        free(groups);
        return FALSE;
    }
    if (groups->GroupCount != expected_count) {
        matched = FALSE;
    }
    if (expected_count == 1 && groups->GroupCount == 1) {
        matched = expected_sid != NULL && EqualSid(groups->Groups[0].Sid, expected_sid);
    }
    emit_u32("W5_GATE17_RESTRICTED_SID_COUNT=", groups->GroupCount);
    emit_bool("W5_GATE17_RESTRICTED_SID_MATCH=", matched);
    free(groups);
    return TRUE;
}

static int launch_child(
    HANDLE token,
    const wchar_t *child_path,
    const wchar_t *cwd
) {
    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    wchar_t command_line[32768];
    DWORD wait_result;
    DWORD exit_code = 0;
    int written = _snwprintf_s(
        command_line,
        sizeof(command_line) / sizeof(command_line[0]),
        _TRUNCATE,
        L"\"%ls\"",
        child_path
    );
    if (written < 0) {
        emit_ascii("W5_GATE17_CHILD_CREATE=FAIL\n");
        emit_u32("W5_GATE17_CHILD_CREATE_ERROR=", ERROR_INSUFFICIENT_BUFFER);
        return 41;
    }
    ZeroMemory(&startup, sizeof(startup));
    ZeroMemory(&process, sizeof(process));
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES_FLAG;
    startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    if (!CreateProcessAsUserW(
        token,
        child_path,
        command_line,
        NULL,
        NULL,
        TRUE,
        CREATE_UNICODE_ENVIRONMENT_FLAG | CREATE_NO_WINDOW_FLAG,
        NULL,
        cwd,
        &startup,
        &process
    )) {
        emit_ascii("W5_GATE17_CHILD_CREATE=FAIL\n");
        emit_u32("W5_GATE17_CHILD_CREATE_ERROR=", GetLastError());
        return 42;
    }
    emit_ascii("W5_GATE17_CHILD_CREATE=PASS\n");
    (void)CloseHandle(process.hThread);
    wait_result = WaitForSingleObject(process.hProcess, 20000);
    if (wait_result == WAIT_TIMEOUT_RESULT) {
        emit_ascii("W5_GATE17_CHILD_WAIT=TIMEOUT\n");
        (void)TerminateProcess(process.hProcess, 0xC000013A);
        (void)WaitForSingleObject(process.hProcess, 2000);
        (void)CloseHandle(process.hProcess);
        return 43;
    }
    if (wait_result != WAIT_OBJECT_0_RESULT ||
        !GetExitCodeProcess(process.hProcess, &exit_code)) {
        emit_ascii("W5_GATE17_CHILD_WAIT=FAIL\n");
        emit_u32("W5_GATE17_CHILD_WAIT_ERROR=", GetLastError());
        (void)CloseHandle(process.hProcess);
        return 44;
    }
    emit_u32("W5_GATE17_CHILD_EXIT=", exit_code);
    (void)CloseHandle(process.hProcess);
    return (int)exit_code;
}

int wmain(int argc, wchar_t **argv) {
    const wchar_t *variant;
    const wchar_t *sid_text;
    const wchar_t *child_path;
    const wchar_t *cwd;
    DWORD flags = 0;
    BOOL has_sid = FALSE;
    PSID expected_sid = NULL;
    HANDLE source_token = NULL;
    HANDLE child_token = NULL;
    SID_AND_ATTRIBUTES restricted_sid;
    DWORD expected_count;
    int child_result;

    if (argc < 5 || !variant_flags(argv[1], &flags, &has_sid)) {
        emit_ascii("W5_GATE17_BROKER=INVALID_ARGUMENTS\n");
        return 30;
    }
    variant = argv[1];
    sid_text = argv[2];
    child_path = argv[3];
    cwd = argv[4];
    expected_count = has_sid ? 1 : 0;
    emit_ascii("W5_GATE17_BROKER_STARTED\n");
    emit_u32("W5_GATE17_FLAGS=", flags);

    if (has_sid && !ConvertStringSidToSidW(sid_text, &expected_sid)) {
        emit_ascii("W5_GATE17_TOKEN_CREATE=FAIL\n");
        emit_u32("W5_GATE17_TOKEN_CREATE_ERROR=", GetLastError());
        return 31;
    }
    restricted_sid.Sid = expected_sid;
    restricted_sid.Attributes = 0;
    if (!OpenProcessToken(
        GetCurrentProcess(),
        TOKEN_DUPLICATE_ACCESS | TOKEN_QUERY_ACCESS | TOKEN_ASSIGN_PRIMARY_ACCESS |
            TOKEN_ADJUST_DEFAULT_ACCESS,
        &source_token
    )) {
        emit_ascii("W5_GATE17_TOKEN_CREATE=FAIL\n");
        emit_u32("W5_GATE17_TOKEN_CREATE_ERROR=", GetLastError());
        if (expected_sid != NULL) {
            (void)LocalFree(expected_sid);
        }
        return 32;
    }

    if (wcscmp(variant, L"U") == 0) {
        child_token = source_token;
    } else if (!CreateRestrictedToken(
        source_token,
        flags,
        0,
        NULL,
        0,
        NULL,
        expected_count,
        has_sid ? &restricted_sid : NULL,
        &child_token
    )) {
        emit_ascii("W5_GATE17_TOKEN_CREATE=FAIL\n");
        emit_u32("W5_GATE17_TOKEN_CREATE_ERROR=", GetLastError());
        (void)CloseHandle(source_token);
        if (expected_sid != NULL) {
            (void)LocalFree(expected_sid);
        }
        return 33;
    }
    emit_ascii("W5_GATE17_TOKEN_CREATE=PASS\n");
    emit_bool("W5_GATE17_TOKEN_RESTRICTED=", IsTokenRestricted(child_token));
    if (!inspect_restricted_sids(child_token, expected_sid, expected_count)) {
        emit_ascii("W5_GATE17_TOKEN_INSPECTION=FAIL\n");
        if (child_token != source_token) {
            (void)CloseHandle(child_token);
        }
        (void)CloseHandle(source_token);
        if (expected_sid != NULL) {
            (void)LocalFree(expected_sid);
        }
        return 34;
    }
    emit_ascii("W5_GATE17_TOKEN_INSPECTION=PASS\n");
    child_result = launch_child(child_token, child_path, cwd);
    if (child_token != source_token) {
        (void)CloseHandle(child_token);
    }
    (void)CloseHandle(source_token);
    if (expected_sid != NULL) {
        (void)LocalFree(expected_sid);
    }
    emit_ascii("W5_GATE17_BROKER_FINISHED\n");
    return child_result;
}
