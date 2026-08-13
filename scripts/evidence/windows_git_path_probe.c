#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN

#include <windows.h>
#include <aclapi.h>
#include <sddl.h>
#include <errno.h>
#include <io.h>
#include <process.h>
#include <stdio.h>
#include <wchar.h>

static void json_escape_wide(const wchar_t *value) {
    const wchar_t *cursor = value;
    putchar('"');
    while (*cursor != L'\0') {
        unsigned int ch = (unsigned int)*cursor++;
        if (ch == '"' || ch == '\\') {
            putchar('\\');
            putchar((int)ch);
        } else if (ch == '\r') {
            fputs("\\r", stdout);
        } else if (ch == '\n') {
            fputs("\\n", stdout);
        } else if (ch < 0x20 || ch > 0x7e) {
            printf("\\u%04x", ch & 0xffffU);
        } else {
            putchar((int)ch);
        }
    }
    putchar('"');
}

static void print_create_file_result(const wchar_t *path, DWORD access) {
    HANDLE handle;
    DWORD error;
    SetLastError(ERROR_SUCCESS);
    handle = CreateFileW(
        path,
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        NULL);
    error = handle == INVALID_HANDLE_VALUE ? GetLastError() : ERROR_SUCCESS;
    printf("{\"path\":");
    json_escape_wide(path);
    printf(",\"access\":%lu,\"success\":%s,\"error\":%lu}",
           (unsigned long)access,
           handle != INVALID_HANDLE_VALUE ? "true" : "false",
           (unsigned long)error);
    if (handle != INVALID_HANDLE_VALUE) CloseHandle(handle);
}

static void print_token_facts(void) {
    HANDLE token = NULL;
    DWORD size = 0;
    DWORD is_appcontainer = 0;
    DWORD integrity = 0;
    TOKEN_APPCONTAINER_INFORMATION *package = NULL;
    TOKEN_MANDATORY_LABEL *label = NULL;
    TOKEN_GROUPS *capabilities = NULL;
    LPWSTR sid = NULL;
    PSID all_applications = NULL;
    BOOL all_applications_member = FALSE;
    BOOL in_job = FALSE;
    DWORD capability_index;

    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        GetTokenInformation(token, TokenIsAppContainer, &is_appcontainer,
                            sizeof(is_appcontainer), &size);
        size = 0;
        GetTokenInformation(token, TokenAppContainerSid, NULL, 0, &size);
        package = (TOKEN_APPCONTAINER_INFORMATION *)HeapAlloc(
            GetProcessHeap(), HEAP_ZERO_MEMORY, size);
        if (package != NULL &&
            !GetTokenInformation(token, TokenAppContainerSid, package, size, &size)) {
            HeapFree(GetProcessHeap(), 0, package);
            package = NULL;
        }
        GetTokenInformation(token, TokenIntegrityLevel, NULL, 0, &size);
        label = (TOKEN_MANDATORY_LABEL *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, size);
        if (label != NULL &&
            GetTokenInformation(token, TokenIntegrityLevel, label, size, &size)) {
            DWORD count = *GetSidSubAuthorityCount(label->Label.Sid);
            integrity = *GetSidSubAuthority(label->Label.Sid, count - 1);
        }
        if (package != NULL && package->TokenAppContainer != NULL)
            ConvertSidToStringSidW(package->TokenAppContainer, &sid);
        size = 0;
        GetTokenInformation(token, TokenCapabilities, NULL, 0, &size);
        capabilities = (TOKEN_GROUPS *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, size);
        if (capabilities != NULL)
            GetTokenInformation(token, TokenCapabilities, capabilities, size, &size);
        if (ConvertStringSidToSidW(L"S-1-15-2-1", &all_applications))
            CheckTokenMembership(token, all_applications, &all_applications_member);
    }
    IsProcessInJob(GetCurrentProcess(), NULL, &in_job);
    printf("{\"is_appcontainer\":%s,\"package_sid\":",
           is_appcontainer ? "true" : "false");
    if (sid != NULL) json_escape_wide(sid); else fputs("null", stdout);
    printf(",\"integrity_rid\":%lu,\"in_job\":%s,"
           "\"all_application_packages_member\":%s,\"capability_sids\":[",
           (unsigned long)integrity,
           in_job ? "true" : "false",
           all_applications_member ? "true" : "false");
    if (capabilities != NULL) {
        for (capability_index = 0;
             capability_index < capabilities->GroupCount;
             capability_index++) {
            LPWSTR capability_sid = NULL;
            if (capability_index != 0) putchar(',');
            if (ConvertSidToStringSidW(
                    capabilities->Groups[capability_index].Sid, &capability_sid)) {
                json_escape_wide(capability_sid);
                LocalFree(capability_sid);
            } else {
                fputs("null", stdout);
            }
        }
    }
    printf("]}");
    if (all_applications != NULL) LocalFree(all_applications);
    if (package != NULL) HeapFree(GetProcessHeap(), 0, package);
    if (capabilities != NULL) HeapFree(GetProcessHeap(), 0, capabilities);
    if (sid != NULL) LocalFree(sid);
    if (label != NULL) HeapFree(GetProcessHeap(), 0, label);
    if (token != NULL) CloseHandle(token);
}

static void print_security_descriptor(void) {
    HANDLE handle;
    PSECURITY_DESCRIPTOR descriptor = NULL;
    LPWSTR sddl = NULL;
    DWORD error;
    DWORD information = OWNER_SECURITY_INFORMATION |
                        GROUP_SECURITY_INFORMATION |
                        DACL_SECURITY_INFORMATION |
                        LABEL_SECURITY_INFORMATION;

    SetLastError(ERROR_SUCCESS);
    handle = CreateFileW(
        L"NUL",
        READ_CONTROL,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        NULL);
    if (handle == INVALID_HANDLE_VALUE) {
        printf("{\"open_success\":false,\"open_error\":%lu,\"sddl\":null}",
               (unsigned long)GetLastError());
        return;
    }
    error = GetSecurityInfo(
        handle,
        SE_KERNEL_OBJECT,
        information,
        NULL,
        NULL,
        NULL,
        NULL,
        &descriptor);
    if (error == ERROR_SUCCESS && descriptor != NULL) {
        if (!ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor,
                SDDL_REVISION_1,
                information,
                &sddl,
                NULL)) {
            error = GetLastError();
        }
    }
    printf("{\"open_success\":true,\"query_error\":%lu,\"sddl\":",
           (unsigned long)error);
    if (sddl != NULL) json_escape_wide(sddl); else fputs("null", stdout);
    putchar('}');
    if (sddl != NULL) LocalFree(sddl);
    if (descriptor != NULL) LocalFree(descriptor);
    CloseHandle(handle);
}

static int matrix_mode(void) {
    wchar_t mapping[32768];
    DWORD mapping_length;
    const wchar_t *paths[] = {L"NUL", L"\\\\.\\NUL"};
    const DWORD accesses[] = {GENERIC_READ, GENERIC_WRITE,
                              GENERIC_READ | GENERIC_WRITE};
    size_t path_index;
    size_t access_index;
    BOOL first = TRUE;

    SetLastError(ERROR_SUCCESS);
    mapping_length = QueryDosDeviceW(L"NUL", mapping, ARRAYSIZE(mapping));
    printf("{\"token\":");
    print_token_facts();
    printf(",\"query_dos_device\":{\"success\":%s,\"error\":%lu,\"mapping\":",
           mapping_length != 0 ? "true" : "false",
           (unsigned long)(mapping_length != 0 ? ERROR_SUCCESS : GetLastError()));
    if (mapping_length != 0) json_escape_wide(mapping); else fputs("null", stdout);
    printf("},\"access_matrix\":[");
    for (path_index = 0; path_index < ARRAYSIZE(paths); path_index++) {
        for (access_index = 0; access_index < ARRAYSIZE(accesses); access_index++) {
            if (!first) putchar(',');
            first = FALSE;
            print_create_file_result(paths[path_index], accesses[access_index]);
        }
    }
    printf("],\"security_descriptor\":");
    print_security_descriptor();
    printf("}\n");
    return 0;
}

static int define_mode(void) {
    wchar_t before[32768];
    wchar_t after[32768];
    DWORD before_length;
    DWORD after_length;
    DWORD error;
    BOOL created;
    BOOL removed = FALSE;
    DWORD flags = DDD_RAW_TARGET_PATH | DDD_NO_BROADCAST_SYSTEM;

    before_length = QueryDosDeviceW(L"NUL", before, ARRAYSIZE(before));
    SetLastError(ERROR_SUCCESS);
    created = DefineDosDeviceW(flags, L"NUL", L"\\Device\\Null");
    error = created ? ERROR_SUCCESS : GetLastError();
    after_length = QueryDosDeviceW(L"NUL", after, ARRAYSIZE(after));
    if (created) {
        removed = DefineDosDeviceW(
            flags | DDD_REMOVE_DEFINITION | DDD_EXACT_MATCH_ON_REMOVE,
            L"NUL",
            L"\\Device\\Null");
    }
    printf("{\"before\":");
    if (before_length != 0) json_escape_wide(before); else fputs("null", stdout);
    printf(",\"created\":%s,\"error\":%lu,\"after\":",
           created ? "true" : "false", (unsigned long)error);
    if (after_length != 0) json_escape_wide(after); else fputs("null", stdout);
    printf(",\"cleanup_attempted\":%s,\"cleanup_success\":%s}\n",
           created ? "true" : "false", removed ? "true" : "false");
    return created ? 41 : 0;
}

static int descendant_mode(const wchar_t *report_path) {
    HANDLE file;
    char facts[2048];
    int length;
    DWORD written = 0;
    HANDLE token = NULL;
    DWORD size = 0;
    DWORD is_appcontainer = 0;
    DWORD integrity = 0;
    TOKEN_APPCONTAINER_INFORMATION *package = NULL;
    TOKEN_MANDATORY_LABEL *label = NULL;
    LPWSTR sid = NULL;
    BOOL in_job = FALSE;

    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        GetTokenInformation(token, TokenIsAppContainer, &is_appcontainer,
                            sizeof(is_appcontainer), &size);
        size = 0;
        GetTokenInformation(token, TokenAppContainerSid, NULL, 0, &size);
        package = (TOKEN_APPCONTAINER_INFORMATION *)HeapAlloc(
            GetProcessHeap(), HEAP_ZERO_MEMORY, size);
        if (package != NULL &&
            !GetTokenInformation(token, TokenAppContainerSid, package, size, &size)) {
            HeapFree(GetProcessHeap(), 0, package);
            package = NULL;
        }
        GetTokenInformation(token, TokenIntegrityLevel, NULL, 0, &size);
        label = (TOKEN_MANDATORY_LABEL *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, size);
        if (label != NULL &&
            GetTokenInformation(token, TokenIntegrityLevel, label, size, &size)) {
            DWORD count = *GetSidSubAuthorityCount(label->Label.Sid);
            integrity = *GetSidSubAuthority(label->Label.Sid, count - 1);
        }
        if (package != NULL && package->TokenAppContainer != NULL)
            ConvertSidToStringSidW(package->TokenAppContainer, &sid);
    }
    IsProcessInJob(GetCurrentProcess(), NULL, &in_job);
    length = _snprintf_s(
        facts,
        sizeof(facts),
        _TRUNCATE,
        "{\"is_appcontainer\":%s,\"package_sid\":\"%ls\","
        "\"integrity_rid\":%lu,\"in_job\":%s}\n",
        is_appcontainer ? "true" : "false",
        sid != NULL ? sid : L"",
        (unsigned long)integrity,
        in_job ? "true" : "false");
    file = CreateFileW(report_path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                       FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE || length < 0 ||
        !WriteFile(file, facts, (DWORD)length, &written, NULL) ||
        written != (DWORD)length) {
        if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
        if (package != NULL) HeapFree(GetProcessHeap(), 0, package);
        if (sid != NULL) LocalFree(sid);
        if (label != NULL) HeapFree(GetProcessHeap(), 0, label);
        if (token != NULL) CloseHandle(token);
        return 51;
    }
    CloseHandle(file);
    if (package != NULL) HeapFree(GetProcessHeap(), 0, package);
    if (sid != NULL) LocalFree(sid);
    if (label != NULL) HeapFree(GetProcessHeap(), 0, label);
    if (token != NULL) CloseHandle(token);
    return 0;
}

static void print_path_result(
    const char *name,
    BOOL first,
    BOOL success,
    DWORD error,
    const wchar_t *path) {
    if (!first) putchar(',');
    printf("\"%s\":{\"success\":%s,\"error\":%lu,\"path\":",
           name,
           success ? "true" : "false",
           (unsigned long)error);
    if (success && path != NULL) json_escape_wide(path); else fputs("null", stdout);
    putchar('}');
}

static void query_final_path(
    HANDLE handle,
    const char *name,
    DWORD flags,
    BOOL first) {
    wchar_t path[32768];
    DWORD length;
    DWORD error;

    SetLastError(ERROR_SUCCESS);
    length = GetFinalPathNameByHandleW(handle, path, ARRAYSIZE(path), flags);
    error = length != 0 && length < ARRAYSIZE(path) ? ERROR_SUCCESS : GetLastError();
    print_path_result(
        name,
        first,
        length != 0 && length < ARRAYSIZE(path),
        error,
        path);
}

static void query_file_name_info(
    HANDLE handle,
    const char *name,
    FILE_INFO_BY_HANDLE_CLASS information_class,
    BOOL first) {
    BYTE buffer[sizeof(FILE_NAME_INFO) + sizeof(wchar_t) * 32768];
    FILE_NAME_INFO *information = (FILE_NAME_INFO *)buffer;
    wchar_t path[32768];
    DWORD characters;
    DWORD error;
    BOOL success;

    ZeroMemory(buffer, sizeof(buffer));
    SetLastError(ERROR_SUCCESS);
    success = GetFileInformationByHandleEx(
        handle, information_class, buffer, sizeof(buffer));
    error = success ? ERROR_SUCCESS : GetLastError();
    characters = success ? information->FileNameLength / sizeof(wchar_t) : 0;
    if (success && characters < ARRAYSIZE(path)) {
        memcpy(path, information->FileName, information->FileNameLength);
        path[characters] = L'\0';
    } else if (success) {
        success = FALSE;
        error = ERROR_INSUFFICIENT_BUFFER;
    }
    print_path_result(name, first, success, error, success ? path : NULL);
}

static int path_matrix_mode(void) {
    wchar_t cwd[32768];
    wchar_t long_path[32768];
    DWORD cwd_length;
    DWORD long_length;
    DWORD cwd_error;
    DWORD open_error;
    DWORD long_error;
    HANDLE handle;

    SetLastError(ERROR_SUCCESS);
    cwd_length = GetCurrentDirectoryW(ARRAYSIZE(cwd), cwd);
    cwd_error = cwd_length != 0 && cwd_length < ARRAYSIZE(cwd)
                    ? ERROR_SUCCESS
                    : GetLastError();
    SetLastError(ERROR_SUCCESS);
    handle = cwd_error == ERROR_SUCCESS
                 ? CreateFileW(
                       cwd,
                       0,
                       FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                       NULL,
                       OPEN_EXISTING,
                       FILE_FLAG_BACKUP_SEMANTICS,
                       NULL)
                 : INVALID_HANDLE_VALUE;
    open_error = handle != INVALID_HANDLE_VALUE ? ERROR_SUCCESS : GetLastError();

    SetLastError(ERROR_SUCCESS);
    long_length = cwd_error == ERROR_SUCCESS
                      ? GetLongPathNameW(cwd, long_path, ARRAYSIZE(long_path))
                      : 0;
    long_error = long_length != 0 && long_length < ARRAYSIZE(long_path)
                     ? ERROR_SUCCESS
                     : GetLastError();

    printf("{\"token\":");
    print_token_facts();
    printf(",\"get_current_directory\":{\"success\":%s,\"error\":%lu,\"path\":",
           cwd_error == ERROR_SUCCESS ? "true" : "false",
           (unsigned long)cwd_error);
    if (cwd_error == ERROR_SUCCESS) json_escape_wide(cwd); else fputs("null", stdout);
    printf("},\"cwd_create_file\":{\"success\":%s,\"error\":%lu},\"paths\":{",
           handle != INVALID_HANDLE_VALUE ? "true" : "false",
           (unsigned long)open_error);
    if (handle != INVALID_HANDLE_VALUE) {
        query_final_path(
            handle,
            "normalized_dos",
            FILE_NAME_NORMALIZED | VOLUME_NAME_DOS,
            TRUE);
        query_final_path(
            handle,
            "opened_dos",
            FILE_NAME_OPENED | VOLUME_NAME_DOS,
            FALSE);
        query_final_path(
            handle,
            "normalized_nt",
            FILE_NAME_NORMALIZED | VOLUME_NAME_NT,
            FALSE);
        query_final_path(
            handle,
            "opened_nt",
            FILE_NAME_OPENED | VOLUME_NAME_NT,
            FALSE);
        query_final_path(
            handle,
            "normalized_none",
            FILE_NAME_NORMALIZED | VOLUME_NAME_NONE,
            FALSE);
        query_final_path(
            handle,
            "opened_none",
            FILE_NAME_OPENED | VOLUME_NAME_NONE,
            FALSE);
        query_file_name_info(handle, "file_name_info", FileNameInfo, FALSE);
        query_file_name_info(
            handle,
            "file_normalized_name_info",
            FileNormalizedNameInfo,
            FALSE);
    } else {
        print_path_result("normalized_dos", TRUE, FALSE, open_error, NULL);
        print_path_result("opened_dos", FALSE, FALSE, open_error, NULL);
        print_path_result("normalized_nt", FALSE, FALSE, open_error, NULL);
        print_path_result("opened_nt", FALSE, FALSE, open_error, NULL);
        print_path_result("normalized_none", FALSE, FALSE, open_error, NULL);
        print_path_result("opened_none", FALSE, FALSE, open_error, NULL);
        print_path_result("file_name_info", FALSE, FALSE, open_error, NULL);
        print_path_result("file_normalized_name_info", FALSE, FALSE, open_error, NULL);
    }
    print_path_result(
        "get_long_path_name",
        FALSE,
        long_error == ERROR_SUCCESS,
        long_error,
        long_error == ERROR_SUCCESS ? long_path : NULL);
    printf("}}\n");
    if (handle != INVALID_HANDLE_VALUE) CloseHandle(handle);
    return 0;
}

static int parent_visibility_mode(
    const wchar_t *parent,
    const wchar_t *sibling_name,
    const wchar_t *sibling_file) {
    wchar_t pattern[32768];
    WIN32_FIND_DATAW data;
    HANDLE find;
    HANDLE file;
    BOOL found_sibling = FALSE;
    DWORD enumerate_error;
    DWORD read_error;
    char byte;
    DWORD read = 0;

    _snwprintf_s(pattern, ARRAYSIZE(pattern), _TRUNCATE, L"%s\\*", parent);
    SetLastError(ERROR_SUCCESS);
    find = FindFirstFileW(pattern, &data);
    enumerate_error = find != INVALID_HANDLE_VALUE ? ERROR_SUCCESS : GetLastError();
    if (find != INVALID_HANDLE_VALUE) {
        do {
            if (wcscmp(data.cFileName, sibling_name) == 0) found_sibling = TRUE;
        } while (FindNextFileW(find, &data));
        FindClose(find);
    }
    SetLastError(ERROR_SUCCESS);
    file = CreateFileW(
        sibling_file,
        GENERIC_READ,
        FILE_SHARE_READ,
        NULL,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        NULL);
    read_error = file != INVALID_HANDLE_VALUE ? ERROR_SUCCESS : GetLastError();
    if (file != INVALID_HANDLE_VALUE) {
        if (!ReadFile(file, &byte, 1, &read, NULL)) read_error = GetLastError();
        CloseHandle(file);
    }
    printf(
        "{\"enumerate_parent\":%s,\"enumerate_error\":%lu,"
        "\"sibling_name_visible\":%s,\"sibling_content_read\":%s,"
        "\"sibling_read_error\":%lu}\n",
        enumerate_error == ERROR_SUCCESS ? "true" : "false",
        (unsigned long)enumerate_error,
        found_sibling ? "true" : "false",
        read > 0 ? "true" : "false",
        (unsigned long)read_error);
    return 0;
}

static int closed_stdio_mode(
    const wchar_t *descriptor_text,
    const wchar_t *git_path,
    const wchar_t *report_path) {
    int descriptor = _wtoi(descriptor_text);
    intptr_t child_exit;
    HANDLE report;
    char payload[256];
    int length;
    DWORD written = 0;

    if (descriptor < 0 || descriptor > 2) return 61;
    _close(descriptor);
    child_exit = _wspawnl(_P_WAIT, git_path, git_path, L"--version", NULL);
    length = _snprintf_s(
        payload,
        sizeof(payload),
        _TRUNCATE,
        "{\"closed_fd\":%d,\"child_exit\":%lld,\"errno\":%d}\n",
        descriptor,
        (long long)child_exit,
        errno);
    report = CreateFileW(report_path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                         FILE_ATTRIBUTE_NORMAL, NULL);
    if (report == INVALID_HANDLE_VALUE || length < 0 ||
        !WriteFile(report, payload, (DWORD)length, &written, NULL) ||
        written != (DWORD)length) {
        if (report != INVALID_HANDLE_VALUE) CloseHandle(report);
        return 62;
    }
    CloseHandle(report);
    return child_exit == 0 ? 0 : 63;
}

int wmain(int argc, wchar_t **argv) {
    wchar_t report_path[32768];
    if (argc == 2 && wcscmp(argv[1], L"matrix") == 0) return matrix_mode();
    if (argc == 2 && wcscmp(argv[1], L"define") == 0) return define_mode();
    if (argc == 1 &&
        GetEnvironmentVariableW(
            L"NEURO_DESCENDANT_REPORT", report_path, ARRAYSIZE(report_path)) > 0)
        return descendant_mode(report_path);
    if (argc == 3 && wcscmp(argv[1], L"descendant") == 0)
        return descendant_mode(argv[2]);
    if (argc == 2 && wcscmp(argv[1], L"path-matrix") == 0)
        return path_matrix_mode();
    if (argc == 5 && wcscmp(argv[1], L"parent-visibility") == 0)
        return parent_visibility_mode(argv[2], argv[3], argv[4]);
    if (argc == 5 && wcscmp(argv[1], L"closed-stdio") == 0)
        return closed_stdio_mode(argv[2], argv[3], argv[4]);
    return 2;
}
