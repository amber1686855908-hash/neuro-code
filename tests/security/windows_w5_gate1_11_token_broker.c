#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

#include <windows.h>
#include <sddl.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#define TOKEN_USER_INFORMATION 1
#define TOKEN_GROUPS_INFORMATION 2
#define TOKEN_PRIVILEGES_INFORMATION 3
#define TOKEN_DEFAULT_DACL_INFORMATION 6
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
#define MAX_SID_TEXT 128
#define SECURITY_MAX_SID_SIZE_VALUE 68
#define CHILD_WAIT_BUDGET_MS 15000

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

static void emit_sid(const char *prefix, PSID sid) {
    LPWSTR text = NULL;
    char utf8[MAX_SID_TEXT];
    int converted;
    if (sid == NULL || !ConvertSidToStringSidW(sid, &text) || text == NULL) {
        emit_ascii(prefix);
        emit_ascii("UNAVAILABLE\n");
        return;
    }
    converted = WideCharToMultiByte(
        CP_UTF8,
        0,
        text,
        -1,
        utf8,
        (int)sizeof(utf8),
        NULL,
        NULL
    );
    if (converted <= 0) {
        emit_ascii(prefix);
        emit_ascii("UNAVAILABLE\n");
    } else {
        emit_ascii(prefix);
        emit_ascii(utf8);
        emit_ascii("\n");
    }
    LocalFree(text);
}

static BOOL append_command_line_argument(
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

static BOOL resolve_logon_sid_from_groups(HANDLE token, PSID *sid_out) {
    DWORD required = 0;
    TOKEN_GROUPS *groups = NULL;
    DWORD index;
    DWORD matches = 0;
    PSID found = NULL;
    if (GetTokenInformation(
        token,
        TOKEN_GROUPS_INFORMATION,
        NULL,
        0,
        &required
    ) || GetLastError() != ERROR_INSUFFICIENT_BUFFER || required < sizeof(DWORD)) {
        return FALSE;
    }
    groups = (TOKEN_GROUPS *)malloc(required);
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
    {
        DWORD sid_size = GetLengthSid(found);
        *sid_out = malloc(sid_size);
        if (*sid_out == NULL || !CopySid(sid_size, *sid_out, found)) {
            free(*sid_out);
            *sid_out = NULL;
            free(groups);
            return FALSE;
        }
    }
    free(groups);
    return TRUE;
}

static BOOL create_world_sid(PSID *sid_out) {
    DWORD sid_size = SECURITY_MAX_SID_SIZE_VALUE;
    *sid_out = malloc(sid_size);
    if (*sid_out == NULL || !CreateWellKnownSid(WinWorldSid, NULL, *sid_out, &sid_size)) {
        free(*sid_out);
        *sid_out = NULL;
        return FALSE;
    }
    return TRUE;
}

static BOOL token_user_matches(HANDLE first, HANDLE second) {
    DWORD first_size = 0;
    DWORD second_size = 0;
    TOKEN_USER *first_user = NULL;
    TOKEN_USER *second_user = NULL;
    BOOL result = FALSE;
    if (GetTokenInformation(first, TOKEN_USER_INFORMATION, NULL, 0, &first_size) ||
        GetLastError() != ERROR_INSUFFICIENT_BUFFER ||
        GetTokenInformation(second, TOKEN_USER_INFORMATION, NULL, 0, &second_size) ||
        GetLastError() != ERROR_INSUFFICIENT_BUFFER) {
        return FALSE;
    }
    first_user = (TOKEN_USER *)malloc(first_size);
    second_user = (TOKEN_USER *)malloc(second_size);
    if (first_user != NULL && second_user != NULL &&
        GetTokenInformation(first, TOKEN_USER_INFORMATION, first_user, first_size, &first_size) &&
        GetTokenInformation(second, TOKEN_USER_INFORMATION, second_user, second_size, &second_size)) {
        result = EqualSid(first_user->User.Sid, second_user->User.Sid);
    }
    free(first_user);
    free(second_user);
    return result;
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
    if (GetTokenInformation(
        token,
        TOKEN_RESTRICTED_SIDS_INFORMATION,
        NULL,
        0,
        &required
    ) || GetLastError() != ERROR_INSUFFICIENT_BUFFER || required < sizeof(DWORD)) {
        return FALSE;
    }
    groups = (TOKEN_GROUPS *)malloc(required);
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
    emit_u32("W5_GATE111_RESTRICTED_SID_COUNT=", groups->GroupCount);
    emit_bool("W5_GATE111_RESTRICTED_SID_MATCH=", matched);
    free(groups);
    return TRUE;
}

static BOOL inspect_privileges(HANDLE token) {
    DWORD required = 0;
    TOKEN_PRIVILEGES *privileges = NULL;
    LUID change_notify;
    DWORD index;
    DWORD unexpected_enabled = 0;
    const char *change_state = "ABSENT";
    if (GetTokenInformation(token, TOKEN_PRIVILEGES_INFORMATION, NULL, 0, &required) ||
        GetLastError() != ERROR_INSUFFICIENT_BUFFER || required < sizeof(DWORD) ||
        !LookupPrivilegeValueW(NULL, L"SeChangeNotifyPrivilege", &change_notify)) {
        return FALSE;
    }
    privileges = (TOKEN_PRIVILEGES *)malloc(required);
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
        BOOL is_change = privileges->Privileges[index].Luid.LowPart == change_notify.LowPart &&
            privileges->Privileges[index].Luid.HighPart == change_notify.HighPart;
        if (is_change) {
            change_state = (privileges->Privileges[index].Attributes & SE_PRIVILEGE_ENABLED)
                ? "ENABLED"
                : "DISABLED";
        } else if ((privileges->Privileges[index].Attributes & SE_PRIVILEGE_ENABLED) != 0) {
            ++unexpected_enabled;
        }
    }
    emit_u32("W5_GATE111_TOKEN_PRIVILEGE_COUNT=", privileges->PrivilegeCount);
    emit_u32("W5_GATE111_UNEXPECTED_ENABLED_PRIVILEGES=", unexpected_enabled);
    emit_ascii("W5_GATE111_SE_CHANGE_NOTIFY=");
    emit_ascii(change_state);
    emit_ascii("\n");
    free(privileges);
    return TRUE;
}

static BOOL set_default_dacl(
    HANDLE token,
    const wchar_t *synthetic_sid,
    const wchar_t *logon_sid
) {
    PSECURITY_DESCRIPTOR descriptor = NULL;
    PACL dacl = NULL;
    TOKEN_DEFAULT_DACL token_dacl;
    BOOL dacl_present = FALSE;
    BOOL dacl_defaulted = FALSE;
    wchar_t sddl[512];
    int written = _snwprintf_s(
        sddl,
        sizeof(sddl) / sizeof(sddl[0]),
        _TRUNCATE,
        L"D:(A;;GA;;;WD)(A;;GA;;;%ls)(A;;GA;;;%ls)",
        logon_sid,
        synthetic_sid
    );
    if (written < 0 || !ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        SDDL_REVISION_1,
        &descriptor,
        NULL
    ) || descriptor == NULL) {
        return FALSE;
    }
    if (!GetSecurityDescriptorDacl(descriptor, &dacl_present, &dacl, &dacl_defaulted) ||
        !dacl_present ||
        dacl == NULL) {
        LocalFree(descriptor);
        return FALSE;
    }
    token_dacl.DefaultDacl = dacl;
    written = SetTokenInformation(
        token,
        TOKEN_DEFAULT_DACL_INFORMATION,
        &token_dacl,
        sizeof(token_dacl)
    );
    LocalFree(descriptor);
    return written != 0;
}

static BOOL inspect_default_dacl(HANDLE token, PSID synthetic, PSID logon, PSID world) {
    DWORD required = 0;
    TOKEN_DEFAULT_DACL *token_dacl = NULL;
    ACL_SIZE_INFORMATION info;
    DWORD index;
    BOOL synthetic_found = FALSE;
    BOOL logon_found = FALSE;
    BOOL world_found = FALSE;
    if (GetTokenInformation(token, TOKEN_DEFAULT_DACL_INFORMATION, NULL, 0, &required) ||
        GetLastError() != ERROR_INSUFFICIENT_BUFFER || required < sizeof(TOKEN_DEFAULT_DACL)) {
        return FALSE;
    }
    token_dacl = (TOKEN_DEFAULT_DACL *)malloc(required);
    if (token_dacl == NULL || !GetTokenInformation(
        token,
        TOKEN_DEFAULT_DACL_INFORMATION,
        token_dacl,
        required,
        &required
    ) || token_dacl->DefaultDacl == NULL || !GetAclInformation(
        token_dacl->DefaultDacl,
        &info,
        sizeof(info),
        AclSizeInformation
    )) {
        free(token_dacl);
        return FALSE;
    }
    for (index = 0; index < info.AceCount; ++index) {
        void *ace_pointer = NULL;
        ACCESS_ALLOWED_ACE *ace;
        if (!GetAce(token_dacl->DefaultDacl, index, &ace_pointer) || ace_pointer == NULL) {
            free(token_dacl);
            return FALSE;
        }
        ace = (ACCESS_ALLOWED_ACE *)ace_pointer;
        if (ace->Header.AceType != ACCESS_ALLOWED_ACE_TYPE || ace->Mask != GENERIC_ALL) {
            continue;
        }
        if (EqualSid(&ace->SidStart, synthetic)) {
            synthetic_found = TRUE;
        } else if (EqualSid(&ace->SidStart, logon)) {
            logon_found = TRUE;
        } else if (EqualSid(&ace->SidStart, world)) {
            world_found = TRUE;
        }
    }
    free(token_dacl);
    return info.AceCount == 3 && synthetic_found && logon_found && world_found;
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
    int index;
    DWORD observed = STILL_ACTIVE;
    DWORD waited = 0;
    BOOL created;
    if (!append_command_line_argument(command_line, sizeof(command_line) / sizeof(command_line[0]), &position, child_path)) {
        emit_ascii("W5_GATE111_CHILD_CREATE=FAIL\n");
        return 41;
    }
    for (index = 0; index < child_argc; ++index) {
        if (!append_command_line_argument(command_line, sizeof(command_line) / sizeof(command_line[0]), &position, child_argv[index])) {
            emit_ascii("W5_GATE111_CHILD_CREATE=FAIL\n");
            return 41;
        }
    }
    ZeroMemory(&startup, sizeof(startup));
    ZeroMemory(&process, sizeof(process));
    startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES_FLAG;
    startup.StartupInfo.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.StartupInfo.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup.StartupInfo.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    inherited_handles[0] = startup.StartupInfo.hStdInput;
    inherited_handles[1] = startup.StartupInfo.hStdOutput;
    inherited_handles[2] = startup.StartupInfo.hStdError;
    if (!SetHandleInformation(inherited_handles[0], HANDLE_FLAG_INHERIT_VALUE, HANDLE_FLAG_INHERIT_VALUE) ||
        !SetHandleInformation(inherited_handles[1], HANDLE_FLAG_INHERIT_VALUE, HANDLE_FLAG_INHERIT_VALUE) ||
        !SetHandleInformation(inherited_handles[2], HANDLE_FLAG_INHERIT_VALUE, HANDLE_FLAG_INHERIT_VALUE)) {
        emit_ascii("W5_GATE111_CHILD_CREATE=FAIL\n");
        return 42;
    }
    (void)InitializeProcThreadAttributeList(NULL, 1, 0, &attribute_bytes);
    if (attribute_bytes == 0) {
        emit_ascii("W5_GATE111_CHILD_CREATE=FAIL\n");
        return 42;
    }
    attributes = (LPPROC_THREAD_ATTRIBUTE_LIST)HeapAlloc(GetProcessHeap(), 0, attribute_bytes);
    if (attributes == NULL || !InitializeProcThreadAttributeList(attributes, 1, 0, &attribute_bytes) ||
        !UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST_VALUE,
            inherited_handles, sizeof(inherited_handles), NULL, NULL)) {
        if (attributes != NULL) {
            HeapFree(GetProcessHeap(), 0, attributes);
        }
        emit_ascii("W5_GATE111_CHILD_CREATE=FAIL\n");
        return 42;
    }
    startup.lpAttributeList = attributes;
    emit_ascii("W5_GATE111_CHILD_LAUNCH_ENTER\n");
    created = CreateProcessAsUserW(
        token,
        child_path,
        command_line,
        NULL,
        NULL,
        TRUE,
        CREATE_UNICODE_ENVIRONMENT_FLAG | CREATE_NO_WINDOW_FLAG | EXTENDED_STARTUPINFO_PRESENT_FLAG,
        NULL,
        cwd,
        &startup.StartupInfo,
        &process
    );
    DeleteProcThreadAttributeList(attributes);
    HeapFree(GetProcessHeap(), 0, attributes);
    if (!created) {
        emit_ascii("W5_GATE111_CHILD_CREATE=FAIL\n");
        emit_u32("W5_GATE111_CHILD_CREATE_ERROR=", GetLastError());
        return 42;
    }
    emit_ascii("W5_GATE111_CHILD_LAUNCH_RETURN=PASS\n");
    emit_u32("W5_GATE111_CHILD_PID=", GetProcessId(process.hProcess));
    CloseHandle(process.hThread);
    while (waited < CHILD_WAIT_BUDGET_MS) {
        if (!GetExitCodeProcess(process.hProcess, &observed) || observed != STILL_ACTIVE) {
            break;
        }
        Sleep(100);
        waited += 100;
    }
    if (observed == STILL_ACTIVE) {
        emit_ascii("W5_GATE111_CHILD_WAIT=TIMEOUT\n");
        (void)TerminateProcess(process.hProcess, 0xC000013A);
        CloseHandle(process.hProcess);
        return 43;
    }
    emit_u32("W5_GATE111_CHILD_EXIT=", observed);
    CloseHandle(process.hProcess);
    return (int)observed;
}

static BOOL parse_variant(
    const wchar_t *variant,
    DWORD *count,
    BOOL *with_logon,
    BOOL *with_world
) {
    *count = 1;
    *with_logon = FALSE;
    *with_world = FALSE;
    if (wcscmp(variant, L"SYN") == 0) {
        return TRUE;
    }
    if (wcscmp(variant, L"SYN_LOGON") == 0) {
        *count = 2;
        *with_logon = TRUE;
        return TRUE;
    }
    if (wcscmp(variant, L"SYN_WORLD") == 0) {
        *count = 2;
        *with_world = TRUE;
        return TRUE;
    }
    if (wcscmp(variant, L"SYN_LOGON_WORLD") == 0) {
        *count = 3;
        *with_logon = TRUE;
        *with_world = TRUE;
        return TRUE;
    }
    return FALSE;
}

int wmain(int argc, wchar_t **argv) {
    const wchar_t *synthetic_text;
    const wchar_t *child_path;
    const wchar_t *cwd;
    DWORD restricted_count;
    BOOL with_logon;
    BOOL with_world;
    PSID synthetic = NULL;
    PSID logon = NULL;
    PSID world = NULL;
    PSID expected[3];
    SID_AND_ATTRIBUTES restricted[3];
    HANDLE source_token = NULL;
    HANDLE child_token = NULL;
    DWORD flags = DISABLE_MAX_PRIVILEGE_FLAG | LUA_TOKEN_FLAG | WRITE_RESTRICTED_FLAG;
    wchar_t logon_text[MAX_SID_TEXT];
    wchar_t synthetic_text_copy[MAX_SID_TEXT];
    LPWSTR converted_synthetic = NULL;
    int child_result;
    int index;
    if (argc < 5 || !parse_variant(argv[1], &restricted_count, &with_logon, &with_world)) {
        emit_ascii("W5_GATE111_BROKER_INVALID_ARGUMENTS\n");
        return 30;
    }
    synthetic_text = argv[2];
    child_path = argv[3];
    cwd = argv[4];
    emit_ascii("W5_GATE111_BROKER_STARTED\n");
    emit_u32("W5_GATE111_FLAGS=", flags);
    if (!ConvertStringSidToSidW(synthetic_text, &synthetic) || synthetic == NULL) {
        emit_ascii("W5_GATE111_TOKEN_CREATE=FAIL\n");
        return 31;
    }
    if (!ConvertSidToStringSidW(synthetic, &converted_synthetic) || converted_synthetic == NULL ||
        wcsncpy_s(synthetic_text_copy, MAX_SID_TEXT, converted_synthetic, _TRUNCATE) != 0) {
        LocalFree(converted_synthetic);
        LocalFree(synthetic);
        return 31;
    }
    LocalFree(converted_synthetic);
    if (!OpenProcessToken(
        GetCurrentProcess(),
        TOKEN_DUPLICATE_ACCESS | TOKEN_QUERY_ACCESS | TOKEN_ASSIGN_PRIMARY_ACCESS |
            TOKEN_ADJUST_DEFAULT_ACCESS,
        &source_token
    )) {
        emit_ascii("W5_GATE111_TOKEN_CREATE=FAIL\n");
        LocalFree(synthetic);
        return 32;
    }
    if (!resolve_logon_sid_from_groups(source_token, &logon) || !create_world_sid(&world) ||
        !ConvertSidToStringSidW(logon, &converted_synthetic) || converted_synthetic == NULL ||
        wcsncpy_s(logon_text, MAX_SID_TEXT, converted_synthetic, _TRUNCATE) != 0) {
        emit_ascii("W5_GATE111_LOGON_SID_GROUP_MATCH=FAIL\n");
        if (converted_synthetic != NULL) {
            LocalFree(converted_synthetic);
        }
        CloseHandle(source_token);
        LocalFree(synthetic);
        free(logon);
        free(world);
        return 33;
    }
    LocalFree(converted_synthetic);
    emit_ascii("W5_GATE111_LOGON_SID_GROUP_MATCH=PASS\n");
    emit_sid("W5_GATE111_LOGON_SID=", logon);
    emit_sid("W5_GATE111_WORLD_SID=", world);
    expected[0] = synthetic;
    restricted[0].Sid = synthetic;
    restricted[0].Attributes = 0;
    index = 1;
    if (with_logon) {
        expected[index] = logon;
        restricted[index].Sid = logon;
        restricted[index].Attributes = 0;
        ++index;
    }
    if (with_world) {
        expected[index] = world;
        restricted[index].Sid = world;
        restricted[index].Attributes = 0;
    }
    child_token = NULL;
    if (!CreateRestrictedToken(
        source_token,
        flags,
        0,
        NULL,
        0,
        NULL,
        restricted_count,
        restricted,
        &child_token
    )) {
        emit_ascii("W5_GATE111_TOKEN_CREATE=FAIL\n");
        CloseHandle(source_token);
        LocalFree(synthetic);
        free(logon);
        free(world);
        return 34;
    }
    emit_ascii("W5_GATE111_TOKEN_CREATE=PASS\n");
    if (!set_default_dacl(child_token, synthetic_text_copy, logon_text)) {
        emit_ascii("W5_GATE111_TOKEN_DACL=FAIL\n");
        CloseHandle(child_token);
        CloseHandle(source_token);
        LocalFree(synthetic);
        free(logon);
        free(world);
        return 35;
    }
    emit_ascii("W5_GATE111_TOKEN_DACL=PASS\n");
    emit_ascii("W5_GATE111_DACL_PRINCIPALS=LOGON,WORLD,SYNTHETIC_WRITE\n");
    emit_bool("W5_GATE111_DACL_SEMANTIC_MATCH=", inspect_default_dacl(child_token, synthetic, logon, world));
    emit_bool("W5_GATE111_TOKEN_RESTRICTED=", IsTokenRestricted(child_token));
    emit_bool("W5_GATE111_TOKEN_USER_MATCH=", token_user_matches(source_token, child_token));
    if (!inspect_restricted_sids(child_token, expected, restricted_count)) {
        emit_ascii("W5_GATE111_TOKEN_INSPECTION=FAIL\n");
        CloseHandle(child_token);
        CloseHandle(source_token);
        LocalFree(synthetic);
        free(logon);
        free(world);
        return 36;
    }
    emit_ascii("W5_GATE111_TOKEN_INSPECTION=PASS\n");
    if (!inspect_privileges(child_token)) {
        emit_ascii("W5_GATE111_TOKEN_PRIVILEGES=FAIL\n");
        CloseHandle(child_token);
        CloseHandle(source_token);
        LocalFree(synthetic);
        free(logon);
        free(world);
        return 37;
    }
    emit_ascii("W5_GATE111_TOKEN_PRIVILEGES=PASS\n");
    child_result = launch_child(child_token, child_path, cwd, argc - 5, &argv[5]);
    CloseHandle(child_token);
    CloseHandle(source_token);
    LocalFree(synthetic);
    free(logon);
    free(world);
    emit_ascii("W5_GATE111_BROKER_FINISHED\n");
    return child_result;
}
