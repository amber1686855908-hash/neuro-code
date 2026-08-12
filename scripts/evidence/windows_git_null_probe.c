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
    TOKEN_APPCONTAINER_INFORMATION package;
    TOKEN_MANDATORY_LABEL *label = NULL;
    TOKEN_GROUPS *capabilities = NULL;
    LPWSTR sid = NULL;
    PSID all_applications = NULL;
    BOOL all_applications_member = FALSE;
    BOOL in_job = FALSE;
    DWORD capability_index;

    ZeroMemory(&package, sizeof(package));
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        GetTokenInformation(token, TokenIsAppContainer, &is_appcontainer,
                            sizeof(is_appcontainer), &size);
        size = sizeof(package);
        GetTokenInformation(token, TokenAppContainerSid, &package, size, &size);
        GetTokenInformation(token, TokenIntegrityLevel, NULL, 0, &size);
        label = (TOKEN_MANDATORY_LABEL *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, size);
        if (label != NULL &&
            GetTokenInformation(token, TokenIntegrityLevel, label, size, &size)) {
            DWORD count = *GetSidSubAuthorityCount(label->Label.Sid);
            integrity = *GetSidSubAuthority(label->Label.Sid, count - 1);
        }
        if (package.TokenAppContainer != NULL)
            ConvertSidToStringSidW(package.TokenAppContainer, &sid);
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
    TOKEN_APPCONTAINER_INFORMATION package;
    TOKEN_MANDATORY_LABEL *label = NULL;
    LPWSTR sid = NULL;
    BOOL in_job = FALSE;

    ZeroMemory(&package, sizeof(package));
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        GetTokenInformation(token, TokenIsAppContainer, &is_appcontainer,
                            sizeof(is_appcontainer), &size);
        size = sizeof(package);
        GetTokenInformation(token, TokenAppContainerSid, &package, size, &size);
        GetTokenInformation(token, TokenIntegrityLevel, NULL, 0, &size);
        label = (TOKEN_MANDATORY_LABEL *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, size);
        if (label != NULL &&
            GetTokenInformation(token, TokenIntegrityLevel, label, size, &size)) {
            DWORD count = *GetSidSubAuthorityCount(label->Label.Sid);
            integrity = *GetSidSubAuthority(label->Label.Sid, count - 1);
        }
        if (package.TokenAppContainer != NULL)
            ConvertSidToStringSidW(package.TokenAppContainer, &sid);
    }
    IsProcessInJob(GetCurrentProcess(), NULL, &in_job);
    length = _snprintf_s(
        facts,
        sizeof(facts),
        _TRUNCATE,
        "{\"is_appcontainer\":%s,\"package_sid_present\":%s,"
        "\"integrity_rid\":%lu,\"in_job\":%s}\n",
        is_appcontainer ? "true" : "false",
        sid != NULL ? "true" : "false",
        (unsigned long)integrity,
        in_job ? "true" : "false");
    file = CreateFileW(report_path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                       FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE || length < 0 ||
        !WriteFile(file, facts, (DWORD)length, &written, NULL) ||
        written != (DWORD)length) {
        if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
        if (sid != NULL) LocalFree(sid);
        if (label != NULL) HeapFree(GetProcessHeap(), 0, label);
        if (token != NULL) CloseHandle(token);
        return 51;
    }
    CloseHandle(file);
    if (sid != NULL) LocalFree(sid);
    if (label != NULL) HeapFree(GetProcessHeap(), 0, label);
    if (token != NULL) CloseHandle(token);
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
    if (argc == 5 && wcscmp(argv[1], L"closed-stdio") == 0)
        return closed_stdio_mode(argv[2], argv[3], argv[4]);
    return 2;
}
