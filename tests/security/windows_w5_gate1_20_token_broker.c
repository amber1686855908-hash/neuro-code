#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

/*
 * W5 Gate 1.20 is evidence-only.  This broker is a disposable native
 * controller-side helper.  It creates exactly one 0xD restricted-token
 * variant, launches the copied probe with CreateProcessAsUserW, and reports
 * bounded token/child facts.  It never changes production token policy,
 * filesystem ACLs, registry state, firewall state, or device state.
 */
#include <windows.h>
#include <sddl.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#define TOKEN_USER_INFORMATION 1
#define TOKEN_GROUPS_INFORMATION 2
#define TOKEN_PRIVILEGES_INFORMATION 3
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
#define EXTENDED_STARTUPINFO_PRESENT_FLAG 0x00080000
#define STARTF_USESTDHANDLES_FLAG 0x00000100
#define HANDLE_FLAG_INHERIT_VALUE 0x00000001
#define PROC_THREAD_ATTRIBUTE_HANDLE_LIST_VALUE 0x00020002
#define WAIT_OBJECT_0_RESULT 0x00000000
#define WAIT_TIMEOUT_RESULT 0x00000102
#define SE_GROUP_LOGON_ID_VALUE 0xC0000000UL
#define SECURITY_MAX_SID_SIZE_VALUE 68
#define MAX_SID_TEXT 128
#define MAX_RESTRICTING_SIDS 3
#define CHILD_WAIT_BUDGET_MS 60000

static void emit_ascii(const char *text) {
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output != NULL && output != INVALID_HANDLE_VALUE) {
        (void)WriteFile(output, text, (DWORD)lstrlenA(text), &written, NULL);
    }
}

static void emit_u32(const char *prefix, DWORD value) {
    char line[160];
    (void)snprintf(line, sizeof(line), "%s%lu\n", prefix, (unsigned long)value);
    emit_ascii(line);
}

static void emit_bool(const char *prefix, BOOL value) {
    emit_ascii(prefix);
    emit_ascii(value ? "PASS\n" : "FAIL\n");
}

static BOOL sid_text(PSID sid, char *output, size_t capacity) {
    LPWSTR converted = NULL;
    int written;
    if (sid == NULL || !IsValidSid(sid) || output == NULL || capacity == 0 ||
        !ConvertSidToStringSidW(sid, &converted) || converted == NULL) {
        return FALSE;
    }
    written = WideCharToMultiByte(
        CP_UTF8,
        0,
        converted,
        -1,
        output,
        (int)capacity,
        NULL,
        NULL
    );
    LocalFree(converted);
    return written > 0;
}

static void emit_sid(const char *prefix, PSID sid) {
    char text[MAX_SID_TEXT];
    if (sid_text(sid, text, sizeof(text))) {
        emit_ascii(prefix);
        emit_ascii(text);
        emit_ascii("\n");
    } else {
        emit_ascii(prefix);
        emit_ascii("UNAVAILABLE\n");
    }
}

static BOOL append_argument(
    wchar_t *command_line,
    size_t capacity,
    size_t *position,
    const wchar_t *argument
) {
    size_t length = wcslen(argument);
    size_t index;
    size_t backslashes = 0;
    BOOL quote = length == 0 || wcspbrk(argument, L" \t\"") != NULL;
    if (*position != 0) {
        if (*position + 1 >= capacity) {
            return FALSE;
        }
        command_line[(*position)++] = L' ';
    }
    if (!quote) {
        if (*position + length >= capacity) {
            return FALSE;
        }
        CopyMemory(command_line + *position, argument, (length + 1) * sizeof(wchar_t));
        *position += length;
        return TRUE;
    }
    if (*position + 1 >= capacity) {
        return FALSE;
    }
    command_line[(*position)++] = L'"';
    for (index = 0; index < length; ++index) {
        wchar_t current = argument[index];
        if (current == L'\\') {
            ++backslashes;
            continue;
        }
        if (current == L'"') {
            size_t count = backslashes * 2 + 1;
            if (*position + count + 1 >= capacity) {
                return FALSE;
            }
            while (count > 0) {
                --count;
                command_line[(*position)++] = L'\\';
            }
            command_line[(*position)++] = L'"';
            backslashes = 0;
            continue;
        }
        if (*position + backslashes + 1 >= capacity) {
            return FALSE;
        }
        while (backslashes > 0) {
            --backslashes;
            command_line[(*position)++] = L'\\';
        }
        command_line[(*position)++] = current;
    }
    if (*position + backslashes * 2 + 2 > capacity) {
        return FALSE;
    }
    while (backslashes > 0) {
        --backslashes;
        command_line[(*position)++] = L'\\';
        command_line[(*position)++] = L'\\';
    }
    command_line[(*position)++] = L'"';
    command_line[*position] = L'\0';
    return TRUE;
}

static BOOL copy_sid(PSID source, PSID *destination) {
    DWORD size;
    if (source == NULL || !IsValidSid(source) || destination == NULL) {
        return FALSE;
    }
    size = GetLengthSid(source);
    *destination = malloc(size);
    if (*destination == NULL || !CopySid(size, *destination, source)) {
        free(*destination);
        *destination = NULL;
        return FALSE;
    }
    return TRUE;
}

static BOOL create_well_known(WELL_KNOWN_SID_TYPE type, PSID *sid_out) {
    BYTE buffer[SECURITY_MAX_SID_SIZE_VALUE];
    DWORD size = sizeof(buffer);
    if (sid_out == NULL || !CreateWellKnownSid(type, NULL, buffer, &size)) {
        return FALSE;
    }
    *sid_out = malloc(size);
    if (*sid_out == NULL || !CopySid(size, *sid_out, buffer)) {
        free(*sid_out);
        *sid_out = NULL;
        return FALSE;
    }
    return TRUE;
}

static BOOL resolve_token_user_sid(HANDLE token, PSID *sid_out) {
    DWORD required = 0;
    TOKEN_USER *user = NULL;
    BOOL result = FALSE;
    if (GetTokenInformation(token, TOKEN_USER_INFORMATION, NULL, 0, &required) ||
        GetLastError() != ERROR_INSUFFICIENT_BUFFER || required < sizeof(TOKEN_USER)) {
        return FALSE;
    }
    user = malloc(required);
    if (user == NULL || !GetTokenInformation(
        token,
        TOKEN_USER_INFORMATION,
        user,
        required,
        &required
    )) {
        free(user);
        return FALSE;
    }
    result = copy_sid(user->User.Sid, sid_out);
    free(user);
    return result;
}

static BOOL resolve_logon_sid(HANDLE token, PSID *sid_out) {
    DWORD required = 0;
    TOKEN_GROUPS *groups = NULL;
    DWORD index;
    DWORD matches = 0;
    PSID found = NULL;
    if (GetTokenInformation(token, TOKEN_GROUPS_INFORMATION, NULL, 0, &required) ||
        GetLastError() != ERROR_INSUFFICIENT_BUFFER || required < sizeof(DWORD)) {
        return FALSE;
    }
    groups = malloc(required);
    if (groups == NULL || !GetTokenInformation(
        token,
        TOKEN_GROUPS_INFORMATION,
        groups,
        required,
        &required
    )) {
        free(groups);
        return FALSE;
    }
    for (index = 0; index < groups->GroupCount; ++index) {
        if ((groups->Groups[index].Attributes & SE_GROUP_LOGON_ID_VALUE) ==
            SE_GROUP_LOGON_ID_VALUE) {
            ++matches;
            found = groups->Groups[index].Sid;
        }
    }
    if (matches != 1 || found == NULL || !IsValidSid(found)) {
        free(groups);
        return FALSE;
    }
    if (!copy_sid(found, sid_out)) {
        free(groups);
        return FALSE;
    }
    free(groups);
    return TRUE;
}

static BOOL set_default_dacl(HANDLE token, const wchar_t *synthetic_text, PSID logon) {
    PSECURITY_DESCRIPTOR descriptor = NULL;
    PACL dacl = NULL;
    TOKEN_DEFAULT_DACL token_dacl;
    BOOL present = FALSE;
    BOOL defaulted = FALSE;
    char logon_text[MAX_SID_TEXT];
    wchar_t logon_wide[MAX_SID_TEXT];
    wchar_t sddl[512];
    int written;
    if (!sid_text(logon, logon_text, sizeof(logon_text)) ||
        MultiByteToWideChar(CP_UTF8, 0, logon_text, -1, logon_wide, MAX_SID_TEXT) <= 0) {
        return FALSE;
    }
    written = _snwprintf_s(
        sddl,
        sizeof(sddl) / sizeof(sddl[0]),
        _TRUNCATE,
        L"D:(A;;GA;;;WD)(A;;GA;;;%ls)(A;;GA;;;%ls)",
        logon_wide,
        synthetic_text
    );
    if (written < 0 || !ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        SDDL_REVISION_1,
        &descriptor,
        NULL
    ) || descriptor == NULL) {
        return FALSE;
    }
    if (!GetSecurityDescriptorDacl(descriptor, &present, &dacl, &defaulted) ||
        !present || dacl == NULL) {
        LocalFree(descriptor);
        return FALSE;
    }
    token_dacl.DefaultDacl = dacl;
    present = SetTokenInformation(token, TokenDefaultDacl, &token_dacl, sizeof(token_dacl));
    LocalFree(descriptor);
    return present;
}

static BOOL inspect_restricted_sids(
    HANDLE token,
    PSID *expected,
    DWORD expected_count
) {
    DWORD required = 0;
    TOKEN_GROUPS *groups = NULL;
    DWORD index;
    BOOL matched = TRUE;
    if (GetTokenInformation(token, TOKEN_RESTRICTED_SIDS_INFORMATION, NULL, 0, &required) ||
        GetLastError() != ERROR_INSUFFICIENT_BUFFER || required < sizeof(DWORD)) {
        return FALSE;
    }
    groups = malloc(required);
    if (groups == NULL || !GetTokenInformation(
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
    for (index = 0; index < groups->GroupCount; ++index) {
        char prefix[96];
        (void)snprintf(prefix, sizeof(prefix), "W5_GATE120_RESTRICTED_SID=%lu|", (unsigned long)index);
        emit_sid(prefix, groups->Groups[index].Sid);
    }
    for (index = 0; matched && index < expected_count; ++index) {
        DWORD candidate;
        BOOL found = FALSE;
        for (candidate = 0; candidate < groups->GroupCount; ++candidate) {
            if (EqualSid(groups->Groups[candidate].Sid, expected[index])) {
                found = TRUE;
                break;
            }
        }
        if (!found) {
            matched = FALSE;
        }
    }
    emit_u32("W5_GATE120_RESTRICTED_SID_COUNT=", groups->GroupCount);
    emit_bool("W5_GATE120_RESTRICTED_SID_MATCH=", matched);
    free(groups);
    return TRUE;
}

static BOOL inspect_privileges(HANDLE token) {
    DWORD required = 0;
    TOKEN_PRIVILEGES *privileges = NULL;
    LUID change_notify;
    DWORD index;
    DWORD unexpected_enabled = 0;
    const char *state = "ABSENT";
    if (GetTokenInformation(token, TOKEN_PRIVILEGES_INFORMATION, NULL, 0, &required) ||
        GetLastError() != ERROR_INSUFFICIENT_BUFFER || required < sizeof(DWORD) ||
        !LookupPrivilegeValueW(NULL, L"SeChangeNotifyPrivilege", &change_notify)) {
        return FALSE;
    }
    privileges = malloc(required);
    if (privileges == NULL || !GetTokenInformation(
        token,
        TOKEN_PRIVILEGES_INFORMATION,
        privileges,
        required,
        &required
    )) {
        free(privileges);
        return FALSE;
    }
    for (index = 0; index < privileges->PrivilegeCount; ++index) {
        BOOL is_change_notify =
            privileges->Privileges[index].Luid.LowPart == change_notify.LowPart &&
            privileges->Privileges[index].Luid.HighPart == change_notify.HighPart;
        if (is_change_notify) {
            state = (privileges->Privileges[index].Attributes & SE_PRIVILEGE_ENABLED)
                ? "ENABLED"
                : "DISABLED";
        } else if ((privileges->Privileges[index].Attributes & SE_PRIVILEGE_ENABLED) != 0) {
            ++unexpected_enabled;
        }
    }
    emit_u32("W5_GATE120_UNEXPECTED_ENABLED_PRIVILEGES=", unexpected_enabled);
    emit_ascii("W5_GATE120_SE_CHANGE_NOTIFY=");
    emit_ascii(state);
    emit_ascii("\n");
    free(privileges);
    return TRUE;
}

static BOOL parse_variant(
    const wchar_t *variant,
    DWORD *expected_count,
    BOOL *with_rc,
    BOOL *with_wr,
    BOOL *with_world,
    BOOL *with_aap,
    BOOL *with_arap
) {
    *expected_count = 1;
    *with_rc = FALSE;
    *with_wr = FALSE;
    *with_world = FALSE;
    *with_aap = FALSE;
    *with_arap = FALSE;
    if (wcscmp(variant, L"SYN") == 0) {
        return TRUE;
    }
    if (wcscmp(variant, L"SYN_RC") == 0) {
        *expected_count = 2;
        *with_rc = TRUE;
        return TRUE;
    }
    if (wcscmp(variant, L"SYN_WR") == 0) {
        *expected_count = 2;
        *with_wr = TRUE;
        return TRUE;
    }
    if (wcscmp(variant, L"SYN_RC_WR") == 0) {
        *expected_count = 3;
        *with_rc = TRUE;
        *with_wr = TRUE;
        return TRUE;
    }
    if (wcscmp(variant, L"SYN_WORLD") == 0) {
        *expected_count = 2;
        *with_world = TRUE;
        return TRUE;
    }
    if (wcscmp(variant, L"SYN_AAP") == 0) {
        *expected_count = 2;
        *with_aap = TRUE;
        return TRUE;
    }
    if (wcscmp(variant, L"SYN_ARAP") == 0) {
        *expected_count = 2;
        *with_arap = TRUE;
        return TRUE;
    }
    if (wcscmp(variant, L"SYN_AAP_ARAP") == 0) {
        *expected_count = 3;
        *with_aap = TRUE;
        *with_arap = TRUE;
        return TRUE;
    }
    return FALSE;
}

static int launch_child(
    HANDLE token,
    const wchar_t *child_path,
    const wchar_t *cwd,
    int child_argc,
    wchar_t **child_argv
) {
    STARTUPINFOEXW startup;
    PROCESS_INFORMATION process;
    wchar_t command_line[32768];
    HANDLE inherited_handles[3];
    SIZE_T attribute_bytes = 0;
    LPPROC_THREAD_ATTRIBUTE_LIST attributes = NULL;
    size_t position = 0;
    DWORD waited = 0;
    DWORD exit_code = STILL_ACTIVE;
    int index;
    BOOL created;
    if (!append_argument(
        command_line,
        sizeof(command_line) / sizeof(command_line[0]),
        &position,
        child_path
    )) {
        emit_ascii("W5_GATE120_CHILD_CREATE=FAIL\n");
        return 41;
    }
    for (index = 0; index < child_argc; ++index) {
        if (!append_argument(
            command_line,
            sizeof(command_line) / sizeof(command_line[0]),
            &position,
            child_argv[index]
        )) {
            emit_ascii("W5_GATE120_CHILD_CREATE=FAIL\n");
            return 41;
        }
    }
    ZeroMemory(&startup, sizeof(startup));
    ZeroMemory(&process, sizeof(process));
    startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.StartupInfo.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup.StartupInfo.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    inherited_handles[0] = startup.StartupInfo.hStdInput;
    inherited_handles[1] = startup.StartupInfo.hStdOutput;
    inherited_handles[2] = startup.StartupInfo.hStdError;
    if (!SetHandleInformation(inherited_handles[0], HANDLE_FLAG_INHERIT_VALUE, HANDLE_FLAG_INHERIT_VALUE) ||
        !SetHandleInformation(inherited_handles[1], HANDLE_FLAG_INHERIT_VALUE, HANDLE_FLAG_INHERIT_VALUE) ||
        !SetHandleInformation(inherited_handles[2], HANDLE_FLAG_INHERIT_VALUE, HANDLE_FLAG_INHERIT_VALUE)) {
        emit_ascii("W5_GATE120_CHILD_CREATE=FAIL\n");
        return 42;
    }
    (void)InitializeProcThreadAttributeList(NULL, 1, 0, &attribute_bytes);
    if (attribute_bytes == 0) {
        emit_ascii("W5_GATE120_CHILD_CREATE=FAIL\n");
        return 42;
    }
    attributes = HeapAlloc(GetProcessHeap(), 0, attribute_bytes);
    if (attributes == NULL || !InitializeProcThreadAttributeList(
        attributes,
        1,
        0,
        &attribute_bytes
    ) || !UpdateProcThreadAttribute(
        attributes,
        0,
        PROC_THREAD_ATTRIBUTE_HANDLE_LIST_VALUE,
        inherited_handles,
        sizeof(inherited_handles),
        NULL,
        NULL
    )) {
        if (attributes != NULL) {
            HeapFree(GetProcessHeap(), 0, attributes);
        }
        emit_ascii("W5_GATE120_CHILD_CREATE=FAIL\n");
        return 42;
    }
    startup.lpAttributeList = attributes;
    created = CreateProcessAsUserW(
        token,
        child_path,
        command_line,
        NULL,
        NULL,
        TRUE,
        CREATE_UNICODE_ENVIRONMENT_FLAG | CREATE_NO_WINDOW_FLAG |
            EXTENDED_STARTUPINFO_PRESENT_FLAG,
        NULL,
        cwd,
        &startup.StartupInfo,
        &process
    );
    DeleteProcThreadAttributeList(attributes);
    HeapFree(GetProcessHeap(), 0, attributes);
    if (!created) {
        emit_ascii("W5_GATE120_CHILD_CREATE=FAIL\n");
        emit_u32("W5_GATE120_CHILD_CREATE_ERROR=", GetLastError());
        return 42;
    }
    emit_ascii("W5_GATE120_CHILD_CREATE=PASS\n");
    emit_u32("W5_GATE120_CHILD_PID=", GetProcessId(process.hProcess));
    CloseHandle(process.hThread);
    while (waited < CHILD_WAIT_BUDGET_MS) {
        if (!GetExitCodeProcess(process.hProcess, &exit_code) || exit_code != STILL_ACTIVE) {
            break;
        }
        Sleep(100);
        waited += 100;
    }
    if (exit_code == STILL_ACTIVE) {
        emit_ascii("W5_GATE120_CHILD_WAIT=TIMEOUT\n");
        (void)TerminateProcess(process.hProcess, 0xC000013A);
        CloseHandle(process.hProcess);
        return 43;
    }
    emit_u32("W5_GATE120_CHILD_EXIT=", exit_code);
    CloseHandle(process.hProcess);
    return (int)exit_code;
}

int wmain(int argc, wchar_t **argv) {
    const wchar_t *variant;
    const wchar_t *synthetic_text;
    const wchar_t *child_path;
    const wchar_t *cwd;
    DWORD expected_count;
    BOOL with_rc;
    BOOL with_wr;
    BOOL with_world;
    BOOL with_aap;
    BOOL with_arap;
    DWORD flags = DISABLE_MAX_PRIVILEGE_FLAG | LUA_TOKEN_FLAG | WRITE_RESTRICTED_FLAG;
    PSID synthetic = NULL;
    PSID restricted_code = NULL;
    PSID write_restricted_code = NULL;
    PSID world = NULL;
    PSID all_app_packages = NULL;
    PSID all_restricted_app_packages = NULL;
    PSID expected[MAX_RESTRICTING_SIDS];
    SID_AND_ATTRIBUTES restricted[MAX_RESTRICTING_SIDS];
    PSID logon = NULL;
    HANDLE source_token = NULL;
    HANDLE child_token = NULL;
    int child_result;
    DWORD index = 0;
    if (argc < 5 || !parse_variant(
        argv[1],
        &expected_count,
        &with_rc,
        &with_wr,
        &with_world,
        &with_aap,
        &with_arap
    )) {
        emit_ascii("W5_GATE120_BROKER=INVALID_ARGUMENTS\n");
        return 30;
    }
    variant = argv[1];
    synthetic_text = argv[2];
    child_path = argv[3];
    cwd = argv[4];
    emit_ascii("W5_GATE120_BROKER_STARTED\n");
    emit_u32("W5_GATE120_FLAGS=", flags);
    emit_ascii("W5_GATE120_VARIANT=");
    {
        char utf8[64];
        int written = WideCharToMultiByte(CP_UTF8, 0, variant, -1, utf8, sizeof(utf8), NULL, NULL);
        if (written > 0) {
            emit_ascii(utf8);
        }
    }
    emit_ascii("\n");
    if (!ConvertStringSidToSidW(synthetic_text, &synthetic) || synthetic == NULL ||
        !create_well_known(WinRestrictedCodeSid, &restricted_code) ||
        !create_well_known(WinWriteRestrictedCodeSid, &write_restricted_code) ||
        !create_well_known(WinWorldSid, &world) ||
        !ConvertStringSidToSidW(L"S-1-15-2-1", &all_app_packages) ||
        !ConvertStringSidToSidW(L"S-1-15-2-2", &all_restricted_app_packages)) {
        emit_ascii("W5_GATE120_TOKEN_CREATE=FAIL\n");
        goto cleanup;
    }
    emit_sid("W5_GATE120_RC_SID=", restricted_code);
    emit_sid("W5_GATE120_WR_SID=", write_restricted_code);
    emit_sid("W5_GATE120_WORLD_SID=", world);
    emit_sid("W5_GATE120_AAP_SID=", all_app_packages);
    emit_sid("W5_GATE120_ARAP_SID=", all_restricted_app_packages);
    if (!OpenProcessToken(
        GetCurrentProcess(),
        TOKEN_DUPLICATE_ACCESS | TOKEN_QUERY_ACCESS | TOKEN_ASSIGN_PRIMARY_ACCESS |
            TOKEN_ADJUST_DEFAULT_ACCESS,
        &source_token
    ) || !resolve_logon_sid(source_token, &logon)) {
        emit_ascii("W5_GATE120_TOKEN_CREATE=FAIL\n");
        goto cleanup;
    }
    expected[index] = synthetic;
    restricted[index].Sid = synthetic;
    restricted[index].Attributes = 0;
    ++index;
    if (with_rc) {
        expected[index] = restricted_code;
        restricted[index].Sid = restricted_code;
        restricted[index].Attributes = 0;
        ++index;
    }
    if (with_wr) {
        expected[index] = write_restricted_code;
        restricted[index].Sid = write_restricted_code;
        restricted[index].Attributes = 0;
        ++index;
    }
    if (with_world) {
        expected[index] = world;
        restricted[index].Sid = world;
        restricted[index].Attributes = 0;
        ++index;
    }
    if (with_aap) {
        expected[index] = all_app_packages;
        restricted[index].Sid = all_app_packages;
        restricted[index].Attributes = 0;
        ++index;
    }
    if (with_arap) {
        expected[index] = all_restricted_app_packages;
        restricted[index].Sid = all_restricted_app_packages;
        restricted[index].Attributes = 0;
        ++index;
    }
    if (index != expected_count) {
        emit_ascii("W5_GATE120_TOKEN_CREATE=FAIL\n");
        emit_u32("W5_GATE120_TOKEN_CREATE_ERROR=", ERROR_INVALID_PARAMETER);
        goto cleanup;
    }
    if (!CreateRestrictedToken(
        source_token,
        flags,
        0,
        NULL,
        0,
        NULL,
        expected_count,
        restricted,
        &child_token
    )) {
        emit_ascii("W5_GATE120_TOKEN_CREATE=FAIL\n");
        emit_u32("W5_GATE120_TOKEN_CREATE_ERROR=", GetLastError());
        goto cleanup;
    }
    emit_ascii("W5_GATE120_TOKEN_CREATE=PASS\n");
    if (!set_default_dacl(child_token, synthetic_text, logon)) {
        emit_ascii("W5_GATE120_TOKEN_DACL=FAIL\n");
        goto cleanup;
    }
    emit_ascii("W5_GATE120_TOKEN_DACL=PASS\n");
    emit_bool("W5_GATE120_TOKEN_RESTRICTED=", IsTokenRestricted(child_token));
    if (!inspect_restricted_sids(child_token, expected, expected_count)) {
        emit_ascii("W5_GATE120_TOKEN_INSPECTION=FAIL\n");
        goto cleanup;
    }
    emit_ascii("W5_GATE120_TOKEN_INSPECTION=PASS\n");
    if (!inspect_privileges(child_token)) {
        emit_ascii("W5_GATE120_TOKEN_PRIVILEGES=FAIL\n");
        goto cleanup;
    }
    emit_ascii("W5_GATE120_TOKEN_PRIVILEGES=PASS\n");
    child_result = launch_child(child_token, child_path, cwd, argc - 5, &argv[5]);
    emit_ascii("W5_GATE120_BROKER_FINISHED\n");
    if (child_token != NULL) {
        CloseHandle(child_token);
        child_token = NULL;
    }
    goto result;

cleanup:
    child_result = 31;
result:
    if (child_token != NULL) {
        CloseHandle(child_token);
    }
    if (source_token != NULL) {
        CloseHandle(source_token);
    }
    if (synthetic != NULL) {
        LocalFree(synthetic);
    }
    free(restricted_code);
    free(write_restricted_code);
    free(world);
    if (all_app_packages != NULL) {
        LocalFree(all_app_packages);
    }
    if (all_restricted_app_packages != NULL) {
        LocalFree(all_restricted_app_packages);
    }
    free(logon);
    return child_result;
}
