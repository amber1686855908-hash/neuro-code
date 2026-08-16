#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

#include <windows.h>
#include <sddl.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#define TOKEN_USER_INFORMATION 1
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
#define TOKEN_LOGON_SID_INFORMATION 28
#define TOKEN_DEFAULT_DACL_INFORMATION 6
#define SECURITY_MAX_SID_SIZE_VALUE 68
#ifdef NEURO_GATE18
#define CREATE_SUSPENDED_FLAG 0x00000004
#define JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS 9
#define JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE_VALUE 0x00002000
#define CHILD_WAIT_TIMEOUT_MS 10000
#elif defined(NEURO_GATE110)
#define CHILD_WAIT_TIMEOUT_MS 15000
#elif defined(NEURO_GATE19)
#define CHILD_WAIT_TIMEOUT_MS 10000
#else
#define CHILD_WAIT_TIMEOUT_MS 20000
#endif

#ifdef NEURO_GATE18
#define GATE_MARKER(name) "W5_GATE18_" name
#elif defined(NEURO_GATE110)
#define GATE_MARKER(name) "W5_GATE110_" name
#elif defined(NEURO_GATE19)
#define GATE_MARKER(name) "W5_GATE19_" name
#else
#define GATE_MARKER(name) "W5_GATE17_" name
#endif

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
    } else if (wcscmp(variant, L"DLR") == 0) {
        *flags = DISABLE_MAX_PRIVILEGE_FLAG | LUA_TOKEN_FLAG;
        *has_sid = TRUE;
    } else if (wcscmp(variant, L"DLW0") == 0) {
        *flags = DISABLE_MAX_PRIVILEGE_FLAG | LUA_TOKEN_FLAG | WRITE_RESTRICTED_FLAG;
        *has_sid = FALSE;
    } else if (wcscmp(variant, L"DLWR") == 0) {
        *flags = DISABLE_MAX_PRIVILEGE_FLAG | LUA_TOKEN_FLAG | WRITE_RESTRICTED_FLAG;
        *has_sid = TRUE;
#ifdef NEURO_GATE110
    } else if (wcscmp(variant, L"PROD_SYN") == 0) {
        *flags = DISABLE_MAX_PRIVILEGE_FLAG | LUA_TOKEN_FLAG | WRITE_RESTRICTED_FLAG;
        *has_sid = TRUE;
    } else if (wcscmp(variant, L"PROD_SYN_WRC") == 0) {
        *flags = DISABLE_MAX_PRIVILEGE_FLAG | LUA_TOKEN_FLAG | WRITE_RESTRICTED_FLAG;
        *has_sid = TRUE;
#endif
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
    emit_u32(GATE_MARKER("RESTRICTED_SID_COUNT="), groups->GroupCount);
    emit_bool(GATE_MARKER("RESTRICTED_SID_MATCH="), matched);
    free(groups);
    return TRUE;
}

#ifdef NEURO_GATE110
static BOOL inspect_restricted_sids_gate110(
    HANDLE token,
    PSID *expected_sids,
    DWORD expected_count
) {
    DWORD required = 0;
    TOKEN_GROUPS *groups = NULL;
    BOOL matched = TRUE;
    DWORD index;
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
    for (index = 0; matched && index < expected_count; ++index) {
        BOOL found = FALSE;
        DWORD candidate;
        for (candidate = 0; candidate < groups->GroupCount; ++candidate) {
            if (EqualSid(groups->Groups[candidate].Sid, expected_sids[index])) {
                found = TRUE;
                break;
            }
        }
        if (!found) {
            matched = FALSE;
        }
    }
    emit_u32(GATE_MARKER("RESTRICTED_SID_COUNT="), groups->GroupCount);
    emit_bool(GATE_MARKER("RESTRICTED_SID_MATCH="), matched);
    free(groups);
    return TRUE;
}

static BOOL emit_write_restricted_code_sid(PSID *sid_out) {
    BYTE sid_buffer[SECURITY_MAX_SID_SIZE_VALUE];
    DWORD sid_size = sizeof(sid_buffer);
    LPWSTR sid_text = NULL;
    if (!CreateWellKnownSid(
        WinWriteRestrictedCodeSid,
        NULL,
        sid_buffer,
        &sid_size
    ) || !ConvertSidToStringSidW((PSID)sid_buffer, &sid_text) || sid_text == NULL) {
        emit_ascii(GATE_MARKER("WRC_CREATE=FAIL\n"));
        emit_u32(GATE_MARKER("WRC_CREATE_ERROR="), GetLastError());
        return FALSE;
    }
    emit_ascii(GATE_MARKER("WRC_TYPE=WinWriteRestrictedCodeSid\n"));
    emit_ascii(GATE_MARKER("WRC_SID="));
    emit_ascii("S-1-5-33\n");
    emit_bool(GATE_MARKER("WRC_CANONICAL_MATCH="), wcscmp(sid_text, L"S-1-5-33") == 0);
    *sid_out = (PSID)malloc(sid_size);
    if (*sid_out == NULL) {
        LocalFree(sid_text);
        emit_ascii(GATE_MARKER("WRC_CREATE=FAIL\n"));
        return FALSE;
    }
    CopyMemory(*sid_out, sid_buffer, sid_size);
    LocalFree(sid_text);
    emit_ascii(GATE_MARKER("WRC_CREATE=PASS\n"));
    return TRUE;
}
#endif

static BOOL inspect_token_privileges(HANDLE token) {
    DWORD required = 0;
    TOKEN_PRIVILEGES *privileges = NULL;
    LUID change_notify;
    DWORD index;
    DWORD unexpected_enabled = 0;
    const char *change_notify_state = "ABSENT";
    if (GetTokenInformation(
        token,
        TOKEN_PRIVILEGES_INFORMATION,
        NULL,
        0,
        &required
    ) || GetLastError() != ERROR_INSUFFICIENT_BUFFER || required < sizeof(DWORD)) {
        return FALSE;
    }
    privileges = (TOKEN_PRIVILEGES *)malloc(required);
    if (privileges == NULL) {
        return FALSE;
    }
    if (!GetTokenInformation(
        token,
        TOKEN_PRIVILEGES_INFORMATION,
        privileges,
        required,
        &required
    ) || !LookupPrivilegeValueW(NULL, L"SeChangeNotifyPrivilege", &change_notify)) {
        free(privileges);
        return FALSE;
    }
    for (index = 0; index < privileges->PrivilegeCount; ++index) {
        BOOL is_change_notify =
            privileges->Privileges[index].Luid.LowPart == change_notify.LowPart &&
            privileges->Privileges[index].Luid.HighPart == change_notify.HighPart;
        if (is_change_notify) {
            change_notify_state = (privileges->Privileges[index].Attributes & 0x00000002)
                ? "ENABLED"
                : "DISABLED";
        } else if ((privileges->Privileges[index].Attributes & 0x00000002) != 0) {
            ++unexpected_enabled;
        }
    }
    emit_u32(GATE_MARKER("TOKEN_PRIVILEGE_COUNT="), privileges->PrivilegeCount);
    emit_u32(GATE_MARKER("UNEXPECTED_ENABLED_PRIVILEGES="), unexpected_enabled);
    emit_ascii(GATE_MARKER("SE_CHANGE_NOTIFY="));
    emit_ascii(change_notify_state);
    emit_ascii("\n");
    emit_ascii(GATE_MARKER("TOKEN_PRIVILEGES=PASS\n"));
    free(privileges);
    return TRUE;
}

#if defined(NEURO_GATE19) || defined(NEURO_GATE110)
static BOOL get_current_logon_sid_text(
    HANDLE token,
    wchar_t *sid_text,
    DWORD sid_capacity
) {
    DWORD required = 0;
    TOKEN_GROUPS *groups = NULL;
    LPWSTR converted = NULL;
    BOOL result = FALSE;
    if (GetTokenInformation(
        token,
        TOKEN_LOGON_SID_INFORMATION,
        NULL,
        0,
        &required
    ) || GetLastError() != ERROR_INSUFFICIENT_BUFFER || required < sizeof(DWORD)) {
        return FALSE;
    }
    groups = (TOKEN_GROUPS *)malloc(required);
    if (groups == NULL || !GetTokenInformation(
        token,
        TOKEN_LOGON_SID_INFORMATION,
        groups,
        required,
        &required
    ) || groups->GroupCount < 1 || groups->Groups[0].Sid == NULL ||
        !ConvertSidToStringSidW(groups->Groups[0].Sid, &converted) || converted == NULL) {
        if (groups != NULL) {
            free(groups);
        }
        return FALSE;
    }
    if (wcsncpy_s(sid_text, sid_capacity, converted, _TRUNCATE) == 0) {
        result = TRUE;
    }
    LocalFree(converted);
    free(groups);
    return result;
}

static BOOL set_broker_default_dacl(
    HANDLE token,
    const wchar_t *write_sid_text,
    const wchar_t *logon_sid_text
) {
    PSECURITY_DESCRIPTOR descriptor = NULL;
    PACL dacl = NULL;
    BOOL dacl_present = FALSE;
    BOOL dacl_defaulted = FALSE;
    TOKEN_DEFAULT_DACL token_dacl;
    wchar_t sddl[512];
    BOOL converted;
    BOOL result;
    int written = _snwprintf_s(
        sddl,
        sizeof(sddl) / sizeof(sddl[0]),
        _TRUNCATE,
        L"D:(A;;GA;;;WD)(A;;GA;;;%ls)(A;;GA;;;%ls)",
        logon_sid_text,
        write_sid_text
    );
    if (written < 0) {
        return FALSE;
    }
    converted = ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        SDDL_REVISION_1,
        &descriptor,
        NULL
    );
    if (!converted || descriptor == NULL) {
        return FALSE;
    }
    result = GetSecurityDescriptorDacl(
        descriptor,
        &dacl_present,
        &dacl,
        &dacl_defaulted
    );
    if (!result || !dacl_present || dacl == NULL) {
        (void)LocalFree(descriptor);
        return FALSE;
    }
    token_dacl.DefaultDacl = dacl;
    result = SetTokenInformation(
        token,
        TokenDefaultDacl,
        &token_dacl,
        sizeof(token_dacl)
    );
    (void)LocalFree(descriptor);
    return result;
}
#else
static BOOL set_broker_default_dacl(HANDLE token, const wchar_t *sid_text) {
    PSECURITY_DESCRIPTOR descriptor = NULL;
    PACL dacl = NULL;
    BOOL dacl_present = FALSE;
    BOOL dacl_defaulted = FALSE;
    TOKEN_DEFAULT_DACL token_dacl;
    wchar_t sddl[256];
    BOOL converted;
    BOOL result;
    int written = _snwprintf_s(
        sddl,
        sizeof(sddl) / sizeof(sddl[0]),
        _TRUNCATE,
        sid_text[0] == L'\0'
            ? L"D:(A;;GA;;;WD)"
            : L"D:(A;;GA;;;WD)(A;;GA;;;%ls)",
        sid_text
    );
    if (written < 0) {
        return FALSE;
    }
    converted = ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        SDDL_REVISION_1,
        &descriptor,
        NULL
    );
    if (!converted || descriptor == NULL) {
        return FALSE;
    }
    result = GetSecurityDescriptorDacl(
        descriptor,
        &dacl_present,
        &dacl,
        &dacl_defaulted
    );
    if (!result || !dacl_present || dacl == NULL) {
        (void)LocalFree(descriptor);
        return FALSE;
    }
    token_dacl.DefaultDacl = dacl;
    result = SetTokenInformation(
        token,
        TokenDefaultDacl,
        &token_dacl,
        sizeof(token_dacl)
    );
    (void)LocalFree(descriptor);
    return result;
}
#endif

#ifdef NEURO_GATE18
static BOOL write_child_pid_marker(const wchar_t *path, DWORD pid) {
    HANDLE marker = CreateFileW(
        path,
        GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL,
        CREATE_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        NULL
    );
    char text[32];
    DWORD written = 0;
    int length;
    BOOL result;
    if (marker == INVALID_HANDLE_VALUE) {
        return FALSE;
    }
    length = snprintf(text, sizeof(text), "%lu\n", (unsigned long)pid);
    result = length > 0 && WriteFile(marker, text, (DWORD)length, &written, NULL) &&
        written == (DWORD)length;
    (void)CloseHandle(marker);
    return result;
}
#endif

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
            while (count-- != 0) {
                command_line[(*position)++] = L'\\';
            }
            command_line[(*position)++] = L'"';
            backslashes = 0;
            continue;
        }
        if (*position + backslashes + 1 >= capacity) {
            return FALSE;
        }
        while (backslashes-- != 0) {
            command_line[(*position)++] = L'\\';
        }
        command_line[(*position)++] = current;
    }
    if (*position + backslashes * 2 + 2 > capacity) {
        return FALSE;
    }
    while (backslashes-- != 0) {
        command_line[(*position)++] = L'\\';
        command_line[(*position)++] = L'\\';
    }
    command_line[(*position)++] = L'"';
    command_line[*position] = L'\0';
    return TRUE;
}

static int launch_child(
    HANDLE token,
    const wchar_t *child_path,
    const wchar_t *cwd,
    int child_argc,
    wchar_t **child_argv
#ifdef NEURO_GATE18
    ,
    const wchar_t *pid_marker_path
#endif
) {
    STARTUPINFOW startup;
    STARTUPINFOEXW startup_ex;
    PROCESS_INFORMATION process;
    wchar_t command_line[32768];
    HANDLE inherited_handles[3];
    SIZE_T attribute_bytes = 0;
    LPPROC_THREAD_ATTRIBUTE_LIST attributes = NULL;
    DWORD wait_result;
    DWORD exit_code = 0;
    int argument_index;
    BOOL attribute_list_ready = FALSE;
    BOOL created = FALSE;
#ifdef NEURO_GATE18
    HANDLE cleanup_job = NULL;
#endif
    size_t command_position = 0;
    if (!append_command_line_argument(
        command_line,
        sizeof(command_line) / sizeof(command_line[0]),
        &command_position,
        child_path
    )) {
        emit_ascii(GATE_MARKER("CHILD_CREATE=FAIL\n"));
        emit_u32(GATE_MARKER("CHILD_CREATE_ERROR="), ERROR_INSUFFICIENT_BUFFER);
        return 41;
    }
    for (argument_index = 0; argument_index < child_argc; ++argument_index) {
        if (!append_command_line_argument(
            command_line,
            sizeof(command_line) / sizeof(command_line[0]),
            &command_position,
            child_argv[argument_index]
        )) {
            emit_ascii(GATE_MARKER("CHILD_CREATE=FAIL\n"));
            emit_u32(GATE_MARKER("CHILD_CREATE_ERROR="), ERROR_INSUFFICIENT_BUFFER);
            return 41;
        }
    }
    ZeroMemory(&startup_ex, sizeof(startup_ex));
    ZeroMemory(&process, sizeof(process));
    startup = startup_ex.StartupInfo;
    startup.cb = sizeof(startup_ex);
    startup.dwFlags = STARTF_USESTDHANDLES_FLAG;
    startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    startup_ex.StartupInfo = startup;
    inherited_handles[0] = startup.hStdInput;
    inherited_handles[1] = startup.hStdOutput;
    inherited_handles[2] = startup.hStdError;
    if (!SetHandleInformation(
        inherited_handles[0],
        HANDLE_FLAG_INHERIT_VALUE,
        HANDLE_FLAG_INHERIT_VALUE
    ) || !SetHandleInformation(
        inherited_handles[1],
        HANDLE_FLAG_INHERIT_VALUE,
        HANDLE_FLAG_INHERIT_VALUE
    ) || !SetHandleInformation(
        inherited_handles[2],
        HANDLE_FLAG_INHERIT_VALUE,
        HANDLE_FLAG_INHERIT_VALUE
    )) {
        emit_ascii(GATE_MARKER("CHILD_CREATE=FAIL\n"));
        emit_u32(GATE_MARKER("CHILD_CREATE_ERROR="), GetLastError());
        return 42;
    }
    (void)InitializeProcThreadAttributeList(NULL, 1, 0, &attribute_bytes);
    if (attribute_bytes == 0) {
        emit_ascii(GATE_MARKER("CHILD_CREATE=FAIL\n"));
        emit_u32(GATE_MARKER("CHILD_CREATE_ERROR="), GetLastError());
        return 42;
    }
    attributes = (LPPROC_THREAD_ATTRIBUTE_LIST)HeapAlloc(
        GetProcessHeap(),
        0,
        attribute_bytes
    );
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
        emit_ascii(GATE_MARKER("CHILD_CREATE=FAIL\n"));
        emit_u32(GATE_MARKER("CHILD_CREATE_ERROR="), GetLastError());
        if (attributes != NULL) {
            HeapFree(GetProcessHeap(), 0, attributes);
        }
        return 42;
    }
    attribute_list_ready = TRUE;
    startup_ex.lpAttributeList = attributes;
    created = CreateProcessAsUserW(
        token,
        child_path,
        command_line,
        NULL,
        NULL,
        TRUE,
        CREATE_UNICODE_ENVIRONMENT_FLAG | CREATE_NO_WINDOW_FLAG |
            EXTENDED_STARTUPINFO_PRESENT_FLAG
#ifdef NEURO_GATE18
            | CREATE_SUSPENDED_FLAG
#endif
            ,
        NULL,
        cwd,
        &startup_ex.StartupInfo,
        &process
    );
    if (attribute_list_ready) {
        DeleteProcThreadAttributeList(attributes);
    }
    if (attributes != NULL) {
        HeapFree(GetProcessHeap(), 0, attributes);
    }
    if (!created) {
        emit_ascii(GATE_MARKER("CHILD_CREATE=FAIL\n"));
        emit_u32(GATE_MARKER("CHILD_CREATE_ERROR="), GetLastError());
        return 42;
    }
#ifdef NEURO_GATE18
    cleanup_job = CreateJobObjectW(NULL, NULL);
    if (cleanup_job != NULL) {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION job_limits;
        ZeroMemory(&job_limits, sizeof(job_limits));
        job_limits.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE_VALUE;
        if (!SetInformationJobObject(
            cleanup_job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            &job_limits,
            sizeof(job_limits)
        ) || !AssignProcessToJobObject(cleanup_job, process.hProcess)) {
            (void)CloseHandle(cleanup_job);
            cleanup_job = NULL;
        }
    }
    if (ResumeThread(process.hThread) == (DWORD)-1) {
        emit_ascii(GATE_MARKER("CHILD_CREATE=FAIL\n"));
        emit_u32(GATE_MARKER("CHILD_CREATE_ERROR="), GetLastError());
        (void)TerminateProcess(process.hProcess, 0xC000013A);
        (void)WaitForSingleObject(process.hProcess, 2000);
        (void)CloseHandle(process.hProcess);
        if (cleanup_job != NULL) {
            (void)CloseHandle(cleanup_job);
        }
        return 42;
    }
    if (!write_child_pid_marker(pid_marker_path, GetProcessId(process.hProcess))) {
        emit_ascii(GATE_MARKER("CHILD_PID_MARKER=FAIL\n"));
    } else {
        emit_ascii(GATE_MARKER("CHILD_PID_MARKER=PASS\n"));
    }
#endif
#ifdef NEURO_GATE19
    emit_u32(GATE_MARKER("CHILD_PID="), GetProcessId(process.hProcess));
    emit_ascii(
        GATE_MARKER("CHILD_INITIAL_ACTIVE=")
    );
    emit_ascii(
        WaitForSingleObject(process.hProcess, 0) == WAIT_TIMEOUT_RESULT
            ? "PASS\n"
            : "EXITED\n"
    );
    emit_ascii(GATE_MARKER("CHILD_ACTIVE=PASS\n"));
#endif
    emit_ascii(GATE_MARKER("CHILD_CREATE=PASS\n"));
    (void)CloseHandle(process.hThread);
    wait_result = WaitForSingleObject(process.hProcess, CHILD_WAIT_TIMEOUT_MS);
    if (wait_result == WAIT_TIMEOUT_RESULT) {
        emit_ascii(GATE_MARKER("CHILD_WAIT=TIMEOUT\n"));
        emit_ascii(GATE_MARKER("CHILD_CLEANUP=TERMINATE\n"));
        {
#ifdef NEURO_GATE19
            BOOL terminated = TerminateProcess(process.hProcess, 0xC000013A);
            emit_ascii(GATE_MARKER("CHILD_CLEANUP_RESULT="));
            emit_ascii(terminated ? "PASS\n" : "FAIL\n");
#else
            (void)TerminateProcess(process.hProcess, 0xC000013A);
#endif
        }
#ifdef NEURO_GATE18
        if (cleanup_job != NULL) {
            (void)TerminateJobObject(cleanup_job, 0xC000013A);
        }
#endif
        (void)WaitForSingleObject(process.hProcess, 2000);
        (void)CloseHandle(process.hProcess);
#ifdef NEURO_GATE18
        if (cleanup_job != NULL) {
            (void)CloseHandle(cleanup_job);
        }
#endif
        return 43;
    }
    if (wait_result != WAIT_OBJECT_0_RESULT ||
        !GetExitCodeProcess(process.hProcess, &exit_code)) {
        emit_ascii(GATE_MARKER("CHILD_WAIT=FAIL\n"));
        emit_u32(GATE_MARKER("CHILD_WAIT_ERROR="), GetLastError());
        (void)CloseHandle(process.hProcess);
#ifdef NEURO_GATE18
        if (cleanup_job != NULL) {
            (void)CloseHandle(cleanup_job);
        }
#endif
        return 44;
    }
    emit_u32(GATE_MARKER("CHILD_EXIT="), exit_code);
#ifdef NEURO_GATE19
    emit_ascii(GATE_MARKER("CHILD_CLEANUP=NONE\n"));
#endif
    (void)CloseHandle(process.hProcess);
#ifdef NEURO_GATE18
    if (cleanup_job != NULL) {
        (void)CloseHandle(cleanup_job);
    }
#endif
    return (int)exit_code;
}

int wmain(int argc, wchar_t **argv) {
    const wchar_t *variant;
    const wchar_t *sid_text;
    const wchar_t *child_path;
    const wchar_t *cwd;
#ifdef NEURO_GATE110
    int child_argc;
    wchar_t **child_argv;
#endif
#ifdef NEURO_GATE18
    const wchar_t *pid_marker_path;
#endif
    DWORD flags = 0;
    BOOL has_sid = FALSE;
    PSID expected_sid = NULL;
#ifdef NEURO_GATE110
    PSID write_restricted_sid = NULL;
    PSID expected_sids[2];
    SID_AND_ATTRIBUTES restricted_sids[2];
#endif
    HANDLE source_token = NULL;
    HANDLE child_token = NULL;
#ifndef NEURO_GATE110
    SID_AND_ATTRIBUTES restricted_sid;
#endif
    DWORD expected_count;
    int child_result;
#if defined(NEURO_GATE19) || defined(NEURO_GATE110)
    wchar_t logon_sid_text[128];
#endif

    if (
#ifdef NEURO_GATE18
        argc < 6 ||
#elif defined(NEURO_GATE110)
        argc < 5 ||
#else
        argc < 5 ||
#endif
        !variant_flags(argv[1], &flags, &has_sid)
    ) {
        emit_ascii(GATE_MARKER("BROKER=INVALID_ARGUMENTS\n"));
        return 30;
    }
    variant = argv[1];
    sid_text = argv[2];
    child_path = argv[3];
    cwd = argv[4];
#ifdef NEURO_GATE110
    child_argc = argc - 5;
    child_argv = &argv[5];
#endif
#ifdef NEURO_GATE18
    pid_marker_path = argv[5];
#endif
#ifdef NEURO_GATE110
    expected_count = wcscmp(variant, L"PROD_SYN_WRC") == 0 ? 2 : 1;
#else
    expected_count = has_sid ? 1 : 0;
#endif
    emit_ascii(GATE_MARKER("BROKER_STARTED\n"));
    emit_u32(GATE_MARKER("FLAGS="), flags);

    if (has_sid && !ConvertStringSidToSidW(sid_text, &expected_sid)) {
        emit_ascii(GATE_MARKER("TOKEN_CREATE=FAIL\n"));
        emit_u32(GATE_MARKER("TOKEN_CREATE_ERROR="), GetLastError());
        return 31;
    }
#ifdef NEURO_GATE110
    if (!emit_write_restricted_code_sid(&write_restricted_sid)) {
        if (expected_sid != NULL) {
            (void)LocalFree(expected_sid);
        }
        return 31;
    }
    expected_sids[0] = expected_sid;
    expected_sids[1] = write_restricted_sid;
    restricted_sids[0].Sid = expected_sid;
    restricted_sids[0].Attributes = 0;
    restricted_sids[1].Sid = write_restricted_sid;
    restricted_sids[1].Attributes = 0;
#else
    restricted_sid.Sid = expected_sid;
    restricted_sid.Attributes = 0;
#endif
    if (!OpenProcessToken(
        GetCurrentProcess(),
        TOKEN_DUPLICATE_ACCESS | TOKEN_QUERY_ACCESS | TOKEN_ASSIGN_PRIMARY_ACCESS |
            TOKEN_ADJUST_DEFAULT_ACCESS,
        &source_token
    )) {
        emit_ascii(GATE_MARKER("TOKEN_CREATE=FAIL\n"));
        emit_u32(GATE_MARKER("TOKEN_CREATE_ERROR="), GetLastError());
        if (expected_sid != NULL) {
            (void)LocalFree(expected_sid);
        }
        return 32;
    }

    if (wcscmp(variant, L"U") == 0) {
        child_token = source_token;
#ifdef NEURO_GATE110
    } else if (!CreateRestrictedToken(
        source_token,
        flags,
        0,
        NULL,
        0,
        NULL,
        expected_count,
        restricted_sids,
        &child_token
    )) {
#else
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
#endif
        emit_ascii(GATE_MARKER("TOKEN_CREATE=FAIL\n"));
        emit_u32(GATE_MARKER("TOKEN_CREATE_ERROR="), GetLastError());
        (void)CloseHandle(source_token);
        if (expected_sid != NULL) {
            (void)LocalFree(expected_sid);
        }
#ifdef NEURO_GATE110
        if (write_restricted_sid != NULL) {
            free(write_restricted_sid);
        }
#endif
        return 33;
    }
    emit_ascii(GATE_MARKER("TOKEN_CREATE=PASS\n"));
#if defined(NEURO_GATE19) || defined(NEURO_GATE110)
    if (!get_current_logon_sid_text(source_token, logon_sid_text, sizeof(logon_sid_text) / sizeof(logon_sid_text[0])) ||
        !set_broker_default_dacl(child_token, sid_text, logon_sid_text)) {
        emit_ascii(GATE_MARKER("TOKEN_DACL=FAIL\n"));
        emit_u32(GATE_MARKER("TOKEN_DACL_ERROR="), GetLastError());
        if (child_token != source_token) {
            (void)CloseHandle(child_token);
        }
        (void)CloseHandle(source_token);
        if (expected_sid != NULL) {
            (void)LocalFree(expected_sid);
        }
#ifdef NEURO_GATE110
        if (write_restricted_sid != NULL) {
            free(write_restricted_sid);
        }
#endif
        return 34;
    }
    emit_ascii(GATE_MARKER("DACL_PRINCIPALS=LOGON,WORLD,SYNTHETIC_WRITE\n"));
#else
    if (!set_broker_default_dacl(child_token, has_sid ? sid_text : L"")) {
        emit_ascii(GATE_MARKER("TOKEN_DACL=FAIL\n"));
        emit_u32(GATE_MARKER("TOKEN_DACL_ERROR="), GetLastError());
        if (child_token != source_token) {
            (void)CloseHandle(child_token);
        }
        (void)CloseHandle(source_token);
        if (expected_sid != NULL) {
            (void)LocalFree(expected_sid);
        }
        return 34;
    }
#endif
    emit_ascii(GATE_MARKER("TOKEN_DACL=PASS\n"));
    emit_bool(GATE_MARKER("TOKEN_RESTRICTED="), IsTokenRestricted(child_token));
#ifdef NEURO_GATE110
    if (!inspect_restricted_sids_gate110(child_token, expected_sids, expected_count)) {
#else
    if (!inspect_restricted_sids(child_token, expected_sid, expected_count)) {
#endif
        emit_ascii(GATE_MARKER("TOKEN_INSPECTION=FAIL\n"));
        if (child_token != source_token) {
            (void)CloseHandle(child_token);
        }
        (void)CloseHandle(source_token);
        if (expected_sid != NULL) {
            (void)LocalFree(expected_sid);
        }
#ifdef NEURO_GATE110
        if (write_restricted_sid != NULL) {
            free(write_restricted_sid);
        }
#endif
        return 34;
    }
    emit_ascii(GATE_MARKER("TOKEN_INSPECTION=PASS\n"));
    if (!inspect_token_privileges(child_token)) {
        emit_ascii(GATE_MARKER("TOKEN_PRIVILEGES=FAIL\n"));
        if (child_token != source_token) {
            (void)CloseHandle(child_token);
        }
        (void)CloseHandle(source_token);
        if (expected_sid != NULL) {
            (void)LocalFree(expected_sid);
        }
#ifdef NEURO_GATE110
        if (write_restricted_sid != NULL) {
            free(write_restricted_sid);
        }
#endif
        return 34;
    }
    child_result = launch_child(
        child_token,
        child_path,
        cwd,
#ifdef NEURO_GATE110
        child_argc,
        child_argv
#else
        0,
        NULL
#endif
#ifdef NEURO_GATE18
        ,
        pid_marker_path
#endif
    );
    if (child_token != source_token) {
        (void)CloseHandle(child_token);
    }
    (void)CloseHandle(source_token);
    if (expected_sid != NULL) {
        (void)LocalFree(expected_sid);
    }
#ifdef NEURO_GATE110
    if (write_restricted_sid != NULL) {
        free(write_restricted_sid);
    }
#endif
    emit_ascii(GATE_MARKER("BROKER_FINISHED\n"));
    return child_result;
}
