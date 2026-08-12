#include <windows.h>
#include <sddl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

static BOOL write_all(HANDLE handle, const void *buffer, DWORD size) {
    const BYTE *cursor = (const BYTE *)buffer;
    while (size > 0) {
        DWORD written = 0;
        if (!WriteFile(handle, cursor, size, &written, NULL) || written == 0) return FALSE;
        cursor += written;
        size -= written;
    }
    return TRUE;
}

static BOOL write_utf8_file(const wchar_t *path, const char *text) {
    HANDLE handle = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                                FILE_ATTRIBUTE_NORMAL, NULL);
    BOOL ok;
    if (handle == INVALID_HANDLE_VALUE) return FALSE;
    ok = write_all(handle, text, (DWORD)strlen(text));
    if (ok) ok = FlushFileBuffers(handle);
    CloseHandle(handle);
    return ok;
}

static BOOL read_exact_file(const wchar_t *path, const char *expected) {
    char buffer[256];
    DWORD read = 0;
    HANDLE handle = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE |
                                FILE_SHARE_DELETE, NULL, OPEN_EXISTING,
                                FILE_ATTRIBUTE_NORMAL, NULL);
    BOOL ok;
    if (handle == INVALID_HANDLE_VALUE) return FALSE;
    ok = ReadFile(handle, buffer, sizeof(buffer) - 1, &read, NULL);
    CloseHandle(handle);
    if (!ok) return FALSE;
    buffer[read] = '\0';
    return strcmp(buffer, expected) == 0;
}

static DWORD denied_create(const wchar_t *path, DWORD access, DWORD disposition, DWORD flags) {
    HANDLE handle = CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE |
                                FILE_SHARE_DELETE, NULL, disposition, flags, NULL);
    DWORD error;
    if (handle != INVALID_HANDLE_VALUE) {
        CloseHandle(handle);
        return ERROR_SUCCESS;
    }
    error = GetLastError();
    return error;
}

static BOOL is_denied(DWORD error) {
    return error == ERROR_ACCESS_DENIED || error == ERROR_PRIVILEGE_NOT_HELD;
}

static BOOL token_facts(wchar_t *sid_text, DWORD sid_chars, DWORD *integrity,
                        BOOL *is_appcontainer, BOOL *in_job) {
    HANDLE token = NULL;
    DWORD size = 0;
    TOKEN_APPCONTAINER_INFORMATION *app = NULL;
    TOKEN_MANDATORY_LABEL *label = NULL;
    LPWSTR converted = NULL;
    BOOL result = FALSE;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) goto done;
    GetTokenInformation(token, TokenIsAppContainer, is_appcontainer, sizeof(*is_appcontainer), &size);
    GetTokenInformation(token, TokenAppContainerSid, NULL, 0, &size);
    app = (TOKEN_APPCONTAINER_INFORMATION *)malloc(size);
    if (!app || !GetTokenInformation(token, TokenAppContainerSid, app, size, &size)) goto done;
    if (!ConvertSidToStringSidW(app->TokenAppContainer, &converted)) goto done;
    wcsncpy_s(sid_text, sid_chars, converted, _TRUNCATE);
    GetTokenInformation(token, TokenIntegrityLevel, NULL, 0, &size);
    label = (TOKEN_MANDATORY_LABEL *)malloc(size);
    if (!label || !GetTokenInformation(token, TokenIntegrityLevel, label, size, &size)) goto done;
    *integrity = *GetSidSubAuthority(label->Label.Sid,
                                    *GetSidSubAuthorityCount(label->Label.Sid) - 1);
    if (!IsProcessInJob(GetCurrentProcess(), NULL, in_job)) goto done;
    result = TRUE;
done:
    if (converted) LocalFree(converted);
    free(app);
    free(label);
    if (token) CloseHandle(token);
    return result;
}

static void join_path(wchar_t *out, size_t size, const wchar_t *root, const wchar_t *leaf) {
    _snwprintf_s(out, size, _TRUNCATE, L"%ls\\%ls", root, leaf);
}

static int run_ro(const wchar_t *root, const wchar_t *report) {
    wchar_t existing[MAX_PATH * 4], nested[MAX_PATH * 4], created[MAX_PATH * 4];
    wchar_t renamed[MAX_PATH * 4], future[MAX_PATH * 4];
    wchar_t sid[256] = L"";
    DWORD integrity = 0;
    BOOL is_appcontainer = FALSE, in_job = FALSE;
    DWORD create_error, modify_error, rename_error, delete_error, dacl_error;
    BOOL read_existing, read_nested, read_future;
    char json[4096];
    HANDLE handle;
    join_path(existing, _countof(existing), root, L"existing.txt");
    join_path(nested, _countof(nested), root, L"nested\\child.txt");
    join_path(created, _countof(created), root, L"denied-create.txt");
    join_path(renamed, _countof(renamed), root, L"denied-rename.txt");
    join_path(future, _countof(future), root, L"future\\descendant.txt");
    read_existing = read_exact_file(existing, "ro-existing");
    read_nested = read_exact_file(nested, "ro-nested");
    read_future = read_exact_file(future, "ro-future");
    create_error = denied_create(created, GENERIC_WRITE, CREATE_NEW, FILE_ATTRIBUTE_NORMAL);
    modify_error = denied_create(existing, GENERIC_WRITE, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL);
    if (MoveFileExW(existing, renamed, 0)) {
        MoveFileExW(renamed, existing, 0);
        rename_error = ERROR_SUCCESS;
    } else rename_error = GetLastError();
    if (DeleteFileW(existing)) delete_error = ERROR_SUCCESS;
    else delete_error = GetLastError();
    handle = CreateFileW(existing, WRITE_DAC, 0, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (handle == INVALID_HANDLE_VALUE) dacl_error = GetLastError();
    else { dacl_error = ERROR_SUCCESS; CloseHandle(handle); }
    token_facts(sid, _countof(sid), &integrity, &is_appcontainer, &in_job);
    _snprintf_s(json, sizeof(json), _TRUNCATE,
        "{\"mode\":\"ro\",\"read_existing\":%s,\"read_nested\":%s,"
        "\"read_future\":%s,\"create_error\":%lu,\"modify_error\":%lu,"
        "\"rename_error\":%lu,\"delete_error\":%lu,\"write_dac_denied\":%s,"
        "\"is_appcontainer\":%s,\"integrity_rid\":%lu,\"in_job\":%s,"
        "\"sid_present\":%s}\n",
        read_existing ? "true" : "false", read_nested ? "true" : "false",
        read_future ? "true" : "false", create_error, modify_error, rename_error,
        delete_error, is_denied(dacl_error) ? "true" : "false",
        is_appcontainer ? "true" : "false", integrity, in_job ? "true" : "false",
        sid[0] ? "true" : "false");
    return write_utf8_file(report, json) && read_existing && read_nested && read_future &&
           is_denied(create_error) && is_denied(modify_error) && is_denied(rename_error) &&
           is_denied(delete_error) && is_denied(dacl_error) && is_appcontainer &&
           integrity == SECURITY_MANDATORY_LOW_RID && in_job ? 0 : 10;
}

static int run_rw(const wchar_t *root, const wchar_t *report) {
    wchar_t existing[MAX_PATH * 4], created[MAX_PATH * 4], directory[MAX_PATH * 4];
    wchar_t renamed[MAX_PATH * 4], replacement[MAX_PATH * 4], ads[MAX_PATH * 4];
    wchar_t sid[256] = L"";
    DWORD integrity = 0;
    BOOL is_appcontainer = FALSE, in_job = FALSE;
    BOOL read_ok, modify_ok, create_ok, dir_ok, rename_ok, replace_ok, delete_ok, ads_ok;
    DWORD owner_error, dacl_error, label_error;
    HANDLE handle;
    char json[4096];
    join_path(existing, _countof(existing), root, L"existing.txt");
    join_path(created, _countof(created), root, L"created.bin");
    join_path(directory, _countof(directory), root, L"created-dir");
    join_path(renamed, _countof(renamed), root, L"renamed.bin");
    join_path(replacement, _countof(replacement), root, L"replacement.tmp");
    join_path(ads, _countof(ads), root, L"existing.txt:neuro-code-poc2a");
    read_ok = read_exact_file(existing, "rw-existing");
    modify_ok = write_utf8_file(existing, "rw-modified");
    create_ok = write_utf8_file(created, "created");
    dir_ok = CreateDirectoryW(directory, NULL);
    rename_ok = MoveFileExW(created, renamed, 0);
    replace_ok = write_utf8_file(replacement, "replacement") &&
                 ReplaceFileW(existing, replacement, NULL, 0, NULL, NULL);
    delete_ok = DeleteFileW(renamed) && RemoveDirectoryW(directory);
    ads_ok = write_utf8_file(ads, "ads-authorized");
    handle = CreateFileW(existing, WRITE_OWNER, 0, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (handle == INVALID_HANDLE_VALUE) owner_error = GetLastError();
    else { owner_error = ERROR_SUCCESS; CloseHandle(handle); }
    handle = CreateFileW(existing, WRITE_DAC, 0, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (handle == INVALID_HANDLE_VALUE) dacl_error = GetLastError();
    else { dacl_error = ERROR_SUCCESS; CloseHandle(handle); }
    handle = CreateFileW(existing, ACCESS_SYSTEM_SECURITY, 0, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (handle == INVALID_HANDLE_VALUE) label_error = GetLastError();
    else { label_error = ERROR_SUCCESS; CloseHandle(handle); }
    token_facts(sid, _countof(sid), &integrity, &is_appcontainer, &in_job);
    _snprintf_s(json, sizeof(json), _TRUNCATE,
        "{\"mode\":\"rw\",\"read\":%s,\"modify\":%s,\"create_file\":%s,"
        "\"create_directory\":%s,\"rename\":%s,\"replace\":%s,\"delete\":%s,"
        "\"ads_write\":%s,\"owner_error\":%lu,\"dacl_error\":%lu,"
        "\"label_error\":%lu,\"is_appcontainer\":%s,\"integrity_rid\":%lu,"
        "\"in_job\":%s,\"sid_present\":%s}\n",
        read_ok ? "true" : "false", modify_ok ? "true" : "false",
        create_ok ? "true" : "false", dir_ok ? "true" : "false",
        rename_ok ? "true" : "false", replace_ok ? "true" : "false",
        delete_ok ? "true" : "false", ads_ok ? "true" : "false",
        owner_error, dacl_error, label_error, is_appcontainer ? "true" : "false",
        integrity, in_job ? "true" : "false", sid[0] ? "true" : "false");
    return write_utf8_file(report, json) && read_ok && modify_ok && create_ok && dir_ok &&
           rename_ok && replace_ok && delete_ok && ads_ok && is_denied(owner_error) &&
           is_denied(dacl_error) && is_denied(label_error) && is_appcontainer &&
           integrity == SECURITY_MANDATORY_LOW_RID && in_job ? 0 : 11;
}

static int run_link(const wchar_t *path, const wchar_t *report) {
    wchar_t sid[256] = L"";
    DWORD integrity = 0;
    BOOL is_appcontainer = FALSE, in_job = FALSE;
    BOOL readable = read_exact_file(path, "outside-secret");
    DWORD read_error = readable ? ERROR_SUCCESS : GetLastError();
    char json[1024];
    token_facts(sid, _countof(sid), &integrity, &is_appcontainer, &in_job);
    _snprintf_s(json, sizeof(json), _TRUNCATE,
        "{\"mode\":\"reparse\",\"outside_readable\":%s,\"last_error\":%lu,"
        "\"is_appcontainer\":%s,\"integrity_rid\":%lu,\"in_job\":%s}\n",
        readable ? "true" : "false", read_error, is_appcontainer ? "true" : "false",
        integrity, in_job ? "true" : "false");
    return write_utf8_file(report, json) && !readable && is_appcontainer && in_job ? 0 : 12;
}

int wmain(int argc, wchar_t **argv) {
    if (argc != 4) return 2;
    if (wcscmp(argv[1], L"ro") == 0) return run_ro(argv[2], argv[3]);
    if (wcscmp(argv[1], L"rw") == 0) return run_rw(argv[2], argv[3]);
    if (wcscmp(argv[1], L"reparse") == 0) return run_link(argv[2], argv[3]);
    return 3;
}
