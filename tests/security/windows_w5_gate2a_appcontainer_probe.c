#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00
#endif

/*
 * W5 Gate 2A is evidence-only.  This helper is intentionally self-contained:
 * it creates one disposable AppContainer profile, launches one final child
 * with SECURITY_CAPABILITIES + JOB_LIST (and HANDLE_LIST for pipes or
 * PSEUDOCONSOLE for PTY), and reports bounded facts only.  It never changes a
 * system ACL or policy.  ACL changes are limited to disposable fixture paths.
 */
#define WIN32_LEAN_AND_MEAN

#include <windows.h>
#include <aclapi.h>
#include <ConsoleApi2.h>
#include <sddl.h>
#include <strsafe.h>
#include <userenv.h>

#include <bcrypt.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <wchar.h>

#pragma comment(lib, "Advapi32.lib")
#pragma comment(lib, "Userenv.lib")
#pragma warning(disable: 4191)

#define G2A_ATTRIBUTE_SECURITY_CAPABILITIES 0x00020009
#define G2A_ATTRIBUTE_HANDLE_LIST 0x00020002
#define G2A_ATTRIBUTE_JOB_LIST 0x0002000D
#define G2A_ATTRIBUTE_PSEUDOCONSOLE 0x00020016
#define G2A_EXTENDED_STARTUPINFO_PRESENT 0x00080000
#define G2A_CREATE_UNICODE_ENVIRONMENT 0x00000400
#define G2A_CREATE_NO_WINDOW 0x08000000
#define G2A_STARTF_USESTDHANDLES 0x00000100
#define G2A_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 0x00002000
#define G2A_FILE_READ_DATA 0x00000001UL
#define G2A_FILE_WRITE_DATA 0x00000002UL
#define G2A_FILE_APPEND_DATA 0x00000004UL
#define G2A_FILE_READ_EA 0x00000008UL
#define G2A_FILE_WRITE_EA 0x00000010UL
#define G2A_FILE_READ_ATTRIBUTES 0x00000080UL
#define G2A_FILE_WRITE_ATTRIBUTES 0x00000100UL
#define G2A_READ_CONTROL 0x00020000UL
#define G2A_SYNCHRONIZE 0x00100000UL
#define G2A_FILE_GENERIC_READ (STANDARD_RIGHTS_READ | G2A_FILE_READ_DATA | G2A_FILE_READ_ATTRIBUTES | G2A_FILE_READ_EA | G2A_SYNCHRONIZE)
#define G2A_FILE_GENERIC_WRITE (STANDARD_RIGHTS_WRITE | G2A_FILE_WRITE_DATA | G2A_FILE_WRITE_ATTRIBUTES | G2A_FILE_WRITE_EA | G2A_FILE_APPEND_DATA | G2A_SYNCHRONIZE)
#define G2A_FILE_ALL_TEMP (G2A_FILE_GENERIC_READ | G2A_FILE_GENERIC_WRITE | DELETE | FILE_DELETE_CHILD)
#define G2A_TOKEN_USER ((TOKEN_INFORMATION_CLASS)1)
#define G2A_TOKEN_GROUPS ((TOKEN_INFORMATION_CLASS)2)
#define G2A_TOKEN_PRIVILEGES ((TOKEN_INFORMATION_CLASS)3)
#define G2A_TOKEN_RESTRICTED_SIDS ((TOKEN_INFORMATION_CLASS)11)
#define G2A_TOKEN_INTEGRITY_LEVEL ((TOKEN_INFORMATION_CLASS)25)
#define G2A_TOKEN_MANDATORY_POLICY ((TOKEN_INFORMATION_CLASS)27)
#define G2A_TOKEN_IS_APP_CONTAINER ((TOKEN_INFORMATION_CLASS)29)
#define G2A_TOKEN_CAPABILITIES ((TOKEN_INFORMATION_CLASS)30)
#define G2A_TOKEN_APP_CONTAINER_SID ((TOKEN_INFORMATION_CLASS)31)
#define G2A_MAX_TOKEN_BUFFER (64UL * 1024UL)
#define G2A_MAX_OUTPUT (64UL * 1024UL)
#define G2A_WAIT_TIMEOUT 258UL
#define G2A_STILL_ACTIVE 259UL
#define G2A_STATUS_ACCESS_DENIED ((LONG)0xC0000022L)
#define G2A_BCRYPT_USE_SYSTEM_PREFERRED_RNG 0x00000002UL
#define G2A_PIPE_BUFFER 4096UL

typedef LONG G2A_NTSTATUS;

typedef struct _G2A_UNICODE_STRING {
    USHORT Length;
    USHORT MaximumLength;
    PWSTR Buffer;
} G2A_UNICODE_STRING;

typedef struct _G2A_OBJECT_ATTRIBUTES {
    ULONG Length;
    HANDLE RootDirectory;
    G2A_UNICODE_STRING *ObjectName;
    ULONG Attributes;
    PVOID SecurityDescriptor;
    PVOID SecurityQualityOfService;
} G2A_OBJECT_ATTRIBUTES;

typedef struct _G2A_IO_STATUS_BLOCK {
    G2A_NTSTATUS Status;
    ULONG Reserved;
    ULONG_PTR Information;
} G2A_IO_STATUS_BLOCK;

typedef G2A_NTSTATUS (NTAPI *g2a_nt_open_file_fn)(
    PHANDLE FileHandle,
    ACCESS_MASK DesiredAccess,
    G2A_OBJECT_ATTRIBUTES *ObjectAttributes,
    G2A_IO_STATUS_BLOCK *IoStatusBlock,
    ULONG ShareAccess,
    ULONG OpenOptions
);

typedef NTSTATUS (WINAPI *g2a_bcrypt_gen_random_fn)(
    BCRYPT_ALG_HANDLE hAlgorithm,
    PUCHAR pbBuffer,
    ULONG cbBuffer,
    ULONG dwFlags
);

typedef struct _G2A_PROFILE {
    WCHAR Name[128];
    PSID Sid;
    BOOL Created;
} G2A_PROFILE;

typedef enum _G2A_ATTRIBUTE_STAGE {
    G2A_ATTRIBUTE_STAGE_SECURITY = 1,
    G2A_ATTRIBUTE_STAGE_JOB = 2,
    G2A_ATTRIBUTE_STAGE_IO = 3
} G2A_ATTRIBUTE_STAGE;

typedef enum _G2A_PROCESS_API {
    G2A_PROCESS_API_AS_USER = 1,
    G2A_PROCESS_API_CURRENT = 2
} G2A_PROCESS_API;

static void g2a_emit(const char *text) {
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output != NULL && output != INVALID_HANDLE_VALUE && text != NULL) {
        (void)WriteFile(output, text, (DWORD)strlen(text), &written, NULL);
    }
}

static void g2a_emitf(const char *format, ...) {
    char line[2048];
    va_list args;
    va_start(args, format);
    (void)vsnprintf_s(line, sizeof(line), _TRUNCATE, format, args);
    va_end(args);
    g2a_emit(line);
}

static void g2a_u32(const char *prefix, DWORD value) {
    g2a_emitf("%s%lu\n", prefix, (unsigned long)value);
}

static void g2a_i32(const char *prefix, LONG value) {
    g2a_emitf("%s%ld\n", prefix, (long)value);
}

static void g2a_hex32(const char *prefix, ULONG value) {
    g2a_emitf("%s0x%08lX\n", prefix, (unsigned long)value);
}

static void g2a_bool(const char *prefix, BOOL value) {
    g2a_emitf("%s%s\n", prefix, value ? "PASS" : "FAIL");
}

static BOOL g2a_valid_handle(HANDLE handle) {
    return handle != NULL && handle != INVALID_HANDLE_VALUE;
}

static BOOL g2a_sid_text(PSID sid, char *output, size_t capacity) {
    LPWSTR text = NULL;
    int converted;
    if (sid == NULL || !IsValidSid(sid) || output == NULL || capacity == 0 ||
        !ConvertSidToStringSidW(sid, &text) || text == NULL) {
        return FALSE;
    }
    converted = WideCharToMultiByte(CP_UTF8, 0, text, -1, output, (int)capacity, NULL, NULL);
    LocalFree(text);
    return converted > 0;
}

static void g2a_sid(const char *prefix, PSID sid) {
    char text[160];
    if (g2a_sid_text(sid, text, sizeof(text))) {
        g2a_emitf("%s%s\n", prefix, text);
    } else {
        g2a_emitf("%sUNAVAILABLE\n", prefix);
    }
}

static PSID g2a_sid_from_text(const wchar_t *text) {
    PSID sid = NULL;
    if (text == NULL || !ConvertStringSidToSidW(text, &sid)) {
        return NULL;
    }
    return sid;
}

static BOOL g2a_query_token(
    HANDLE token,
    TOKEN_INFORMATION_CLASS information_class,
    BYTE **buffer,
    DWORD *size,
    DWORD *error
) {
    DWORD required = 0;
    BYTE *allocated;
    SetLastError(ERROR_SUCCESS);
    if (GetTokenInformation(token, information_class, NULL, 0, &required)) {
        *error = ERROR_INVALID_DATA;
        return FALSE;
    }
    *error = GetLastError();
    if (*error != ERROR_INSUFFICIENT_BUFFER || required == 0 || required > G2A_MAX_TOKEN_BUFFER) {
        return FALSE;
    }
    allocated = (BYTE *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, required);
    if (allocated == NULL) {
        *error = ERROR_NOT_ENOUGH_MEMORY;
        return FALSE;
    }
    if (!GetTokenInformation(token, information_class, allocated, required, &required)) {
        *error = GetLastError();
        HeapFree(GetProcessHeap(), 0, allocated);
        return FALSE;
    }
    *buffer = allocated;
    *size = required;
    *error = ERROR_SUCCESS;
    return TRUE;
}

static void g2a_emit_token_sid(HANDLE token, TOKEN_INFORMATION_CLASS information_class, const char *prefix) {
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    PSID sid = NULL;
    if (!g2a_query_token(token, information_class, &buffer, &size, &error)) {
        g2a_u32(prefix, error);
        return;
    }
    (void)size;
    if (information_class == G2A_TOKEN_USER) {
        sid = ((TOKEN_USER *)buffer)->User.Sid;
    }
    if (sid != NULL) {
        g2a_sid(prefix, sid);
    } else {
        g2a_emitf("%sUNAVAILABLE\n", prefix);
    }
    HeapFree(GetProcessHeap(), 0, buffer);
}

static void g2a_emit_groups(HANDLE token, TOKEN_INFORMATION_CLASS information_class, const char *prefix) {
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    TOKEN_GROUPS *groups;
    DWORD index;
    if (!g2a_query_token(token, information_class, &buffer, &size, &error)) {
        g2a_u32(prefix, error);
        return;
    }
    (void)size;
    groups = (TOKEN_GROUPS *)buffer;
    g2a_emitf("%sCOUNT=%lu\n", prefix, (unsigned long)groups->GroupCount);
    for (index = 0; index < groups->GroupCount && index < 64; ++index) {
        char sid[160];
        if (g2a_sid_text(groups->Groups[index].Sid, sid, sizeof(sid))) {
            g2a_emitf("%sSID=%s|ATTR=0x%08lX\n", prefix, sid,
                (unsigned long)groups->Groups[index].Attributes);
        }
    }
    HeapFree(GetProcessHeap(), 0, buffer);
}

static void g2a_emit_privileges(HANDLE token) {
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    TOKEN_PRIVILEGES *privileges;
    DWORD index;
    DWORD enabled = 0;
    DWORD unexpected = 0;
    if (!g2a_query_token(token, G2A_TOKEN_PRIVILEGES, &buffer, &size, &error)) {
        g2a_u32("G2A_TOKEN_PRIVILEGE_QUERY_ERROR=", error);
        return;
    }
    (void)size;
    privileges = (TOKEN_PRIVILEGES *)buffer;
    for (index = 0; index < privileges->PrivilegeCount && index < 128; ++index) {
        WCHAR name[128];
        DWORD name_length = (DWORD)(sizeof(name) / sizeof(name[0]));
        if ((privileges->Privileges[index].Attributes & SE_PRIVILEGE_ENABLED) != 0) {
            enabled++;
            if (!LookupPrivilegeNameW(NULL, &privileges->Privileges[index].Luid, name, &name_length) ||
                wcscmp(name, L"SeChangeNotifyPrivilege") != 0) {
                unexpected++;
            }
        }
    }
    g2a_u32("G2A_TOKEN_PRIVILEGE_COUNT=", privileges->PrivilegeCount);
    g2a_u32("G2A_TOKEN_ENABLED_PRIVILEGE_COUNT=", enabled);
    g2a_u32("G2A_TOKEN_UNEXPECTED_ENABLED_PRIVILEGES=", unexpected);
    HeapFree(GetProcessHeap(), 0, buffer);
}

static void g2a_emit_integrity(HANDLE token) {
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    TOKEN_MANDATORY_LABEL *label;
    BYTE count;
    DWORD rid;
    if (!g2a_query_token(token, G2A_TOKEN_INTEGRITY_LEVEL, &buffer, &size, &error)) {
        g2a_u32("G2A_TOKEN_INTEGRITY_ERROR=", error);
        return;
    }
    (void)size;
    label = (TOKEN_MANDATORY_LABEL *)buffer;
    count = *GetSidSubAuthorityCount(label->Label.Sid);
    rid = count == 0 ? 0 : *GetSidSubAuthority(label->Label.Sid, count - 1);
    g2a_u32("G2A_TOKEN_INTEGRITY_RID=", rid);
    HeapFree(GetProcessHeap(), 0, buffer);
}

static void g2a_attest_token(void) {
    HANDLE token = NULL;
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    BOOL is_app = FALSE;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        g2a_u32("G2A_TOKEN_OPEN_ERROR=", GetLastError());
        return;
    }
    g2a_emit_token_sid(token, G2A_TOKEN_USER, "G2A_TOKEN_USER=");
    if (g2a_query_token(token, G2A_TOKEN_IS_APP_CONTAINER, &buffer, &size, &error)) {
        is_app = *(BOOL *)buffer;
        g2a_bool("G2A_TOKEN_IS_APP_CONTAINER=", is_app);
        HeapFree(GetProcessHeap(), 0, buffer);
    } else {
        g2a_u32("G2A_TOKEN_IS_APP_CONTAINER_ERROR=", error);
    }
    if (g2a_query_token(token, G2A_TOKEN_APP_CONTAINER_SID, &buffer, &size, &error)) {
        TOKEN_APPCONTAINER_INFORMATION *info = (TOKEN_APPCONTAINER_INFORMATION *)buffer;
        g2a_sid("G2A_TOKEN_APPCONTAINER_SID=", info->TokenAppContainer);
        HeapFree(GetProcessHeap(), 0, buffer);
    } else {
        g2a_u32("G2A_TOKEN_APPCONTAINER_SID_ERROR=", error);
    }
    g2a_emit_groups(token, G2A_TOKEN_CAPABILITIES, "G2A_TOKEN_CAPABILITY_");
    g2a_emit_groups(token, G2A_TOKEN_RESTRICTED_SIDS, "G2A_TOKEN_RESTRICTED_");
    g2a_emit_groups(token, G2A_TOKEN_GROUPS, "G2A_TOKEN_GROUP_");
    g2a_emit_privileges(token);
    g2a_emit_integrity(token);
    if (g2a_query_token(token, G2A_TOKEN_MANDATORY_POLICY, &buffer, &size, &error)) {
        g2a_hex32("G2A_TOKEN_MANDATORY_POLICY=", ((TOKEN_MANDATORY_POLICY *)buffer)->Policy);
        HeapFree(GetProcessHeap(), 0, buffer);
    } else {
        g2a_u32("G2A_TOKEN_MANDATORY_POLICY_ERROR=", error);
    }
    g2a_bool("G2A_TOKEN_QUERY_CLOSED=", CloseHandle(token));
}

static BOOL g2a_build_object_attributes(
    const wchar_t *object_text,
    G2A_UNICODE_STRING *object_name,
    G2A_OBJECT_ATTRIBUTES *object_attributes
) {
    size_t characters = wcslen(object_text);
    if (characters > 0x7FFF || characters * sizeof(wchar_t) > 0xFFFE) {
        return FALSE;
    }
    object_name->Length = (USHORT)(characters * sizeof(wchar_t));
    object_name->MaximumLength = (USHORT)((characters + 1) * sizeof(wchar_t));
    object_name->Buffer = (PWSTR)object_text;
    ZeroMemory(object_attributes, sizeof(*object_attributes));
    object_attributes->Length = (ULONG)sizeof(*object_attributes);
    object_attributes->ObjectName = object_name;
    object_attributes->Attributes = 0;
    return TRUE;
}

static void g2a_ntopen(void) {
    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    g2a_nt_open_file_fn nt_open;
    const ACCESS_MASK accesses[] = {0x100000, 0x100001, 0x100002, 0x100003};
    const char *names[] = {"0x100000", "0x100001", "0x100002", "0x100003"};
    DWORD index;
    if (ntdll == NULL) {
        g2a_emit("G2A_NTOPEN_AVAILABLE=FAIL\n");
        return;
    }
    nt_open = (g2a_nt_open_file_fn)GetProcAddress(ntdll, "NtOpenFile");
    if (nt_open == NULL) {
        g2a_emit("G2A_NTOPEN_AVAILABLE=FAIL\n");
        return;
    }
    g2a_emit("G2A_NTOPEN_AVAILABLE=PASS\n");
    for (index = 0; index < 4; ++index) {
        WCHAR target[] = L"\\Device\\KsecDD";
        G2A_UNICODE_STRING name;
        G2A_OBJECT_ATTRIBUTES attributes;
        G2A_IO_STATUS_BLOCK io;
        HANDLE handle = NULL;
        G2A_NTSTATUS status;
        if (!g2a_build_object_attributes(target, &name, &attributes)) {
            g2a_emitf("G2A_NTOPEN_%s=0x%08lX\n", names[index], (unsigned long)G2A_STATUS_ACCESS_DENIED);
            continue;
        }
        ZeroMemory(&io, sizeof(io));
        status = nt_open(&handle, accesses[index], &attributes, &io, 0x7, 0x20);
        g2a_emitf("G2A_NTOPEN_%s=0x%08lX\n", names[index], (unsigned long)status);
        if (g2a_valid_handle(handle)) {
            (void)CloseHandle(handle);
        }
    }
}

static void g2a_cng(void) {
    HMODULE bcrypt = LoadLibraryW(L"bcrypt.dll");
    g2a_bool("G2A_BCRYPT_LOAD=", bcrypt != NULL);
    if (bcrypt == NULL) {
        g2a_u32("G2A_BCRYPT_LOAD_ERROR=", GetLastError());
        return;
    }
    {
        g2a_bcrypt_gen_random_fn gen =
            (g2a_bcrypt_gen_random_fn)GetProcAddress(bcrypt, "BCryptGenRandom");
        UCHAR bytes[16];
        NTSTATUS status;
        if (gen == NULL) {
            g2a_emit("G2A_BCRYPT_GEN_RANDOM=UNAVAILABLE\n");
        } else {
            status = gen(NULL, bytes, sizeof(bytes), G2A_BCRYPT_USE_SYSTEM_PREFERRED_RNG);
            g2a_i32("G2A_BCRYPT_GEN_RANDOM=", status);
        }
    }
    (void)FreeLibrary(bcrypt);
}

static BOOL g2a_read_line(HANDLE input, char *buffer, DWORD capacity) {
    DWORD used = 0;
    while (used + 1U < capacity) {
        char byte = 0;
        DWORD received = 0;
        if (!ReadFile(input, &byte, 1, &received, NULL) || received == 0) {
            return FALSE;
        }
        if (byte == '\n' || byte == '\r') {
            buffer[used] = '\0';
            return TRUE;
        }
        buffer[used++] = byte;
    }
    return FALSE;
}

static BOOL g2a_spawn_descendant(void) {
    WCHAR self[MAX_PATH * 4];
    WCHAR command[MAX_PATH * 4 + 64];
    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    DWORD length = GetModuleFileNameW(NULL, self, sizeof(self) / sizeof(self[0]));
    if (length == 0 || length >= sizeof(self) / sizeof(self[0])) {
        g2a_u32("G2A_DESCENDANT_CREATE_ERROR=", GetLastError());
        return FALSE;
    }
    if (swprintf_s(command, sizeof(command) / sizeof(command[0]), L"\"%s\" descendant", self) < 0) {
        g2a_emit("G2A_DESCENDANT_CREATE_ERROR=87\n");
        return FALSE;
    }
    ZeroMemory(&startup, sizeof(startup));
    ZeroMemory(&process, sizeof(process));
    startup.cb = sizeof(startup);
    if (!CreateProcessW(NULL, command, NULL, NULL, FALSE, G2A_CREATE_NO_WINDOW,
        NULL, NULL, &startup, &process)) {
        g2a_u32("G2A_DESCENDANT_CREATE_ERROR=", GetLastError());
        return FALSE;
    }
    g2a_emit("G2A_DESCENDANT_CREATE=PASS\n");
    g2a_u32("G2A_DESCENDANT_PID=", process.dwProcessId);
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return TRUE;
}

static void g2a_fs_result(const char *label, const char *operation, BOOL ok, DWORD error) {
    g2a_emitf("G2A_FS_%s_%s=%s|ERROR=%lu\n", label, operation, ok ? "PASS" : "DENY", (unsigned long)error);
}

static void g2a_fs_open(const wchar_t *path, const char *label, DWORD access, BOOL write) {
    HANDLE handle = CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    DWORD error = handle == INVALID_HANDLE_VALUE ? GetLastError() : ERROR_SUCCESS;
    g2a_fs_result(label, write ? "WRITE" : "READ", handle != INVALID_HANDLE_VALUE, error);
    if (handle != INVALID_HANDLE_VALUE) {
        if (write) {
            const char byte = 'x';
            DWORD written = 0;
            BOOL ok = WriteFile(handle, &byte, 1, &written, NULL) && written == 1;
            g2a_fs_result(label, "WRITE_FILE", ok, ok ? ERROR_SUCCESS : GetLastError());
        }
        (void)CloseHandle(handle);
    }
}

static void g2a_fs_fixture(const wchar_t *root) {
    const struct { const wchar_t *name; const char *label; } files[] = {
        {L"authorized_workspace.txt", "AUTHORIZED_WORKSPACE"},
        {L"outside_user_only.txt", "OUTSIDE_USER_ONLY"},
        {L"appcontainer_sid_only.txt", "APPCONTAINER_SID_ONLY"},
        {L"all_application_packages_only.txt", "ALL_APPLICATION_PACKAGES_ONLY"},
        {L"all_restricted_application_packages_only.txt", "ALL_RESTRICTED_APPLICATION_PACKAGES_ONLY"},
        {L"sensitive_read.txt", "SENSITIVE_READ"},
        {L"read_only.txt", "READ_ONLY"},
    };
    DWORD index;
    wchar_t path[MAX_PATH * 4];
    g2a_emit("G2A_FILESYSTEM_STARTED\n");
    for (index = 0; index < sizeof(files) / sizeof(files[0]); ++index) {
        if (swprintf_s(path, sizeof(path) / sizeof(path[0]), L"%s\\%s", root, files[index].name) < 0) {
            continue;
        }
        g2a_fs_open(path, files[index].label, GENERIC_READ, FALSE);
        g2a_fs_open(path, files[index].label, GENERIC_WRITE, TRUE);
    }
    if (swprintf_s(path, sizeof(path) / sizeof(path[0]), L"%s\\child-created.txt", root) >= 0) {
        HANDLE created = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW,
            FILE_ATTRIBUTE_NORMAL, NULL);
        g2a_fs_result("AUTHORIZED_WORKSPACE", "CREATE", created != INVALID_HANDLE_VALUE,
            created == INVALID_HANDLE_VALUE ? GetLastError() : ERROR_SUCCESS);
        if (created != INVALID_HANDLE_VALUE) {
            (void)CloseHandle(created);
        }
    }
    g2a_emit("G2A_FILESYSTEM_FINISHED\n");
}

static int g2a_descendant(const wchar_t *path) {
    (void)path;
    Sleep(60000);
    return 0;
}

static void g2a_child_common(const wchar_t *fixture_root, HANDLE input, BOOL pty, BOOL minimal) {
    char line[128];
    g2a_emit("G2A_CHILD_STARTED\n");
    g2a_attest_token();
    if (minimal) {
        g2a_emit("G2A_CHILD_MINIMAL=TRUE\n");
        g2a_emit("G2A_CHILD_FINISHED\n");
        return;
    }
    g2a_ntopen();
    g2a_cng();
    if (fixture_root != NULL) {
        g2a_fs_fixture(fixture_root);
    }
    if (input != NULL && g2a_read_line(input, line, sizeof(line))) {
        g2a_emitf("G2A_STDIN=%s\n", strcmp(line, "g2a-input") == 0 ? "PASS" : "UNEXPECTED");
    } else if (pty) {
        g2a_emit("G2A_STDIN=UNAVAILABLE\n");
    }
    (void)g2a_spawn_descendant();
    g2a_emit("G2A_CHILD_FINISHED\n");
}

static int g2a_child_pipe(const wchar_t *fixture_root, BOOL minimal) {
    HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    g2a_child_common(fixture_root, input, FALSE, minimal);
    return 0;
}

static int g2a_child_pty(const wchar_t *fixture_root, BOOL minimal) {
    HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    g2a_child_common(fixture_root, input, TRUE, minimal);
    return 0;
}

static BOOL g2a_create_fixture_file(const wchar_t *path) {
    HANDLE file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) {
        return FALSE;
    }
    {
        const char text[] = "fixture";
        DWORD written = 0;
        (void)WriteFile(file, text, sizeof(text) - 1, &written, NULL);
    }
    (void)CloseHandle(file);
    return TRUE;
}

static BOOL g2a_grant_sid(const wchar_t *path, PSID sid, DWORD mask) {
    PSECURITY_DESCRIPTOR descriptor = NULL;
    PACL old_dacl = NULL;
    PACL new_dacl = NULL;
    EXPLICIT_ACCESSW access;
    DWORD result;
    ZeroMemory(&access, sizeof(access));
    result = GetNamedSecurityInfoW((LPWSTR)path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
        NULL, NULL, &old_dacl, NULL, &descriptor);
    if (result != ERROR_SUCCESS) {
        return FALSE;
    }
    access.grfAccessPermissions = mask;
    access.grfAccessMode = GRANT_ACCESS;
    access.grfInheritance = NO_INHERITANCE;
    access.Trustee.TrusteeForm = TRUSTEE_IS_SID;
    access.Trustee.TrusteeType = TRUSTEE_IS_UNKNOWN;
    access.Trustee.ptstrName = (LPWSTR)sid;
    result = SetEntriesInAclW(1, &access, old_dacl, &new_dacl);
    if (result == ERROR_SUCCESS) {
        result = SetNamedSecurityInfoW((LPWSTR)path, SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION, NULL, NULL, new_dacl, NULL);
    }
    if (new_dacl != NULL) {
        LocalFree(new_dacl);
    }
    if (descriptor != NULL) {
        LocalFree(descriptor);
    }
    return result == ERROR_SUCCESS;
}

static BOOL g2a_prepare_fixtures(const wchar_t *root, PSID app_sid) {
    const wchar_t *names[] = {
        L"authorized_workspace.txt", L"outside_user_only.txt", L"appcontainer_sid_only.txt",
        L"all_application_packages_only.txt", L"all_restricted_application_packages_only.txt",
        L"sensitive_read.txt", L"read_only.txt"
    };
    DWORD index;
    wchar_t path[MAX_PATH * 4];
    PSID aap = g2a_sid_from_text(L"S-1-15-2-1");
    PSID arap = g2a_sid_from_text(L"S-1-15-2-2");
    BOOL ok = CreateDirectoryW(root, NULL) || GetLastError() == ERROR_ALREADY_EXISTS;
    if (!ok || !g2a_grant_sid(root, app_sid, G2A_FILE_ALL_TEMP)) {
        if (aap != NULL) LocalFree(aap);
        if (arap != NULL) LocalFree(arap);
        return FALSE;
    }
    for (index = 0; index < sizeof(names) / sizeof(names[0]); ++index) {
        if (swprintf_s(path, sizeof(path) / sizeof(path[0]), L"%s\\%s", root, names[index]) < 0 ||
            !g2a_create_fixture_file(path)) {
            ok = FALSE;
            continue;
        }
        if (index == 0 || index == 2) {
            ok = ok && g2a_grant_sid(path, app_sid, G2A_FILE_ALL_TEMP);
        } else if (index == 3 && aap != NULL) {
            ok = ok && g2a_grant_sid(path, aap, G2A_FILE_ALL_TEMP);
        } else if (index == 4 && arap != NULL) {
            ok = ok && g2a_grant_sid(path, arap, G2A_FILE_ALL_TEMP);
        } else if (index == 6) {
            ok = ok && g2a_grant_sid(path, app_sid, G2A_FILE_GENERIC_READ);
        }
    }
    if (aap != NULL) LocalFree(aap);
    if (arap != NULL) LocalFree(arap);
    return ok;
}

static BOOL g2a_profile_create(G2A_PROFILE *profile) {
    HRESULT result;
    DWORD pid = GetCurrentProcessId();
    DWORD tick = GetTickCount();
    ZeroMemory(profile, sizeof(*profile));
    if (swprintf_s(profile->Name, sizeof(profile->Name) / sizeof(profile->Name[0]),
        L"NeuroCodeW5Gate2A-%lu-%lu", (unsigned long)pid, (unsigned long)tick) < 0) {
        g2a_emit("G2A_PROFILE_CREATE=FAIL\n");
        g2a_u32("G2A_PROFILE_CREATE_ERROR=", ERROR_INVALID_NAME);
        return FALSE;
    }
    result = CreateAppContainerProfile(profile->Name, profile->Name, L"Neuro Code W5 Gate 2A", NULL, 0, &profile->Sid);
    if (FAILED(result) || profile->Sid == NULL) {
        g2a_emit("G2A_PROFILE_CREATE=FAIL\n");
        g2a_i32("G2A_PROFILE_CREATE_HRESULT=", result);
        return FALSE;
    }
    profile->Created = TRUE;
    g2a_emit("G2A_PROFILE_CREATE=PASS\n");
    g2a_sid("G2A_APP_CONTAINER_SID=", profile->Sid);
    return TRUE;
}

static void g2a_profile_cleanup(G2A_PROFILE *profile) {
    HRESULT result = E_FAIL;
    if (profile->Created) {
        result = DeleteAppContainerProfile(profile->Name);
        g2a_i32("G2A_PROFILE_DELETE_HRESULT=", result);
        g2a_bool("G2A_PROFILE_DELETE=", SUCCEEDED(result));
    } else {
        g2a_emit("G2A_PROFILE_DELETE=NOT_CREATED\n");
    }
    if (profile->Sid != NULL) {
        FreeSid(profile->Sid);
        profile->Sid = NULL;
    }
}

static BOOL g2a_make_job(HANDLE *job) {
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
    *job = CreateJobObjectW(NULL, NULL);
    if (!g2a_valid_handle(*job)) {
        return FALSE;
    }
    ZeroMemory(&limits, sizeof(limits));
    limits.BasicLimitInformation.LimitFlags = G2A_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(*job, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) {
        CloseHandle(*job);
        *job = NULL;
        return FALSE;
    }
    return TRUE;
}

static BOOL g2a_make_pipe(HANDLE *read_handle, HANDLE *write_handle) {
    SECURITY_ATTRIBUTES security;
    ZeroMemory(&security, sizeof(security));
    security.nLength = sizeof(security);
    security.bInheritHandle = TRUE;
    if (!CreatePipe(read_handle, write_handle, &security, 0)) {
        return FALSE;
    }
    return TRUE;
}

static BOOL g2a_noninherit(HANDLE handle) {
    return SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0);
}

static void g2a_emit_wide(const char *prefix, const wchar_t *value) {
    char converted[4096];
    int length;
    if (value == NULL) {
        g2a_emitf("%sNULL\n", prefix);
        return;
    }
    length = WideCharToMultiByte(
        CP_UTF8, 0, value, -1, converted, (int)sizeof(converted), NULL, NULL
    );
    if (length <= 0) {
        g2a_emitf("%sUNAVAILABLE\n", prefix);
        return;
    }
    converted[sizeof(converted) - 1] = '\0';
    g2a_emitf("%s%s\n", prefix, converted);
}

static void g2a_emit_path_full(const char *label, const wchar_t *value) {
    char converted[4096];
    int length;
    if (value == NULL) {
        g2a_emitf("G2A_PATH_%s_FULL=NULL\n", label);
        return;
    }
    length = WideCharToMultiByte(
        CP_UTF8, 0, value, -1, converted, (int)sizeof(converted), NULL, NULL
    );
    if (length <= 0) {
        g2a_emitf("G2A_PATH_%s_FULL=UNAVAILABLE\n", label);
        return;
    }
    converted[sizeof(converted) - 1] = '\0';
    g2a_emitf("G2A_PATH_%s_FULL=%s\n", label, converted);
}

static void g2a_path_fact(const char *label, const wchar_t *path, BOOL executable) {
    WCHAR full[MAX_PATH * 4];
    DWORD attributes;
    DWORD length;
    HANDLE readable;
    DWORD error;
    if (path == NULL || path[0] == L'\0') {
        g2a_emitf("G2A_PATH_%s_FULL=NULL\n", label);
        g2a_emitf("G2A_PATH_%s_EXISTS=UNKNOWN\n", label);
        g2a_emitf("G2A_PATH_%s_TYPE=UNKNOWN\n", label);
        if (executable) {
            g2a_emitf("G2A_PATH_%s_READABLE=UNKNOWN\n", label);
        }
        return;
    }
    length = GetFullPathNameW(path, (DWORD)(sizeof(full) / sizeof(full[0])), full, NULL);
    if (length == 0 || length >= sizeof(full) / sizeof(full[0])) {
        g2a_emitf("G2A_PATH_%s_FULL=UNAVAILABLE\n", label);
        g2a_u32("G2A_PATH_FULL_ERROR=", GetLastError());
        return;
    }
    g2a_emit_path_full(label, full);
    attributes = GetFileAttributesW(full);
    if (attributes == INVALID_FILE_ATTRIBUTES) {
        g2a_emitf("G2A_PATH_%s_EXISTS=NO\n", label);
        g2a_emitf("G2A_PATH_%s_TYPE=MISSING\n", label);
    } else {
        g2a_emitf("G2A_PATH_%s_EXISTS=YES\n", label);
        g2a_emitf(
            "G2A_PATH_%s_TYPE=%s\n",
            label,
            (attributes & FILE_ATTRIBUTE_DIRECTORY) != 0 ? "DIRECTORY" : "FILE"
        );
    }
    if (executable) {
        readable = CreateFileW(
            full,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            NULL,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            NULL
        );
        error = readable == INVALID_HANDLE_VALUE ? GetLastError() : ERROR_SUCCESS;
        g2a_emitf("G2A_PATH_%s_READABLE=%s\n", label, readable == INVALID_HANDLE_VALUE ? "NO" : "YES");
        g2a_u32("G2A_PATH_READABLE_ERROR=", error);
        if (readable != INVALID_HANDLE_VALUE) {
            CloseHandle(readable);
        }
    }
}

static void g2a_path_attestation(
    const wchar_t *self,
    const wchar_t *workspace,
    const wchar_t *fixture_root,
    const wchar_t *current_directory
) {
    WCHAR controller[MAX_PATH * 4];
    DWORD length = GetCurrentDirectoryW(
        (DWORD)(sizeof(controller) / sizeof(controller[0])), controller
    );
    g2a_emit("G2A_PATH_ATTESTATION=STARTED\n");
    g2a_path_fact("SELF", self, TRUE);
    g2a_path_fact("WORKSPACE", workspace, FALSE);
    g2a_path_fact("FIXTURE_ROOT", fixture_root, FALSE);
    if (length == 0 || length >= sizeof(controller) / sizeof(controller[0])) {
        g2a_emit("G2A_CONTROLLER_CURRENT_DIRECTORY=UNAVAILABLE\n");
    } else {
        g2a_emit_wide("G2A_CONTROLLER_CURRENT_DIRECTORY=", controller);
    }
    g2a_emit_wide("G2A_LP_CURRENT_DIRECTORY=", current_directory);
    g2a_emit("G2A_PATH_ATTESTATION=FINISHED\n");
}

static void g2a_emit_environment_path(const char *label, const wchar_t *value) {
    DWORD attributes;
    if (value == NULL || value[0] == L'\0') {
        g2a_emitf("G2A_ENV_%s_EXISTS=UNKNOWN\n", label);
        return;
    }
    attributes = GetFileAttributesW(value);
    if (attributes == INVALID_FILE_ATTRIBUTES) {
        g2a_emitf("G2A_ENV_%s_EXISTS=NO\n", label);
    } else {
        g2a_emitf("G2A_ENV_%s_EXISTS=YES\n", label);
    }
}

static void g2a_emit_environment_value(
    const char *label,
    const wchar_t *value,
    BOOL path
) {
    BOOL present = value != NULL && value[0] != L'\0';
    g2a_emitf("G2A_ENV_%s=%s\n", label, present ? "PRESENT" : "ABSENT");
    if (path) {
        g2a_emit_environment_path(label, value);
    }
}

static const wchar_t *g2a_environment_block_value(
    const wchar_t *block,
    const wchar_t *name
) {
    size_t name_length;
    const wchar_t *cursor;
    if (block == NULL || name == NULL) {
        return NULL;
    }
    name_length = wcslen(name);
    cursor = block;
    while (*cursor != L'\0') {
        if (wcsncmp(cursor, name, name_length) == 0 && cursor[name_length] == L'=') {
            return cursor + name_length + 1;
        }
        cursor += wcslen(cursor) + 1;
    }
    return NULL;
}

static void g2a_emit_environment_facts_from_block(const wchar_t *block) {
    const wchar_t *value;
    value = g2a_environment_block_value(block, L"USERNAME");
    g2a_emit_environment_value("USERNAME", value, FALSE);
    value = g2a_environment_block_value(block, L"USERDOMAIN");
    g2a_emit_environment_value("USERDOMAIN", value, FALSE);
    value = g2a_environment_block_value(block, L"USERPROFILE");
    g2a_emit_environment_value("USERPROFILE", value, TRUE);
    value = g2a_environment_block_value(block, L"LOCALAPPDATA");
    g2a_emit_environment_value("LOCALAPPDATA", value, TRUE);
    value = g2a_environment_block_value(block, L"APPDATA");
    g2a_emit_environment_value("APPDATA", value, FALSE);
    value = g2a_environment_block_value(block, L"TEMP");
    g2a_emit_environment_value("TEMP", value, TRUE);
    value = g2a_environment_block_value(block, L"TMP");
    g2a_emit_environment_value("TMP", value, FALSE);
    value = g2a_environment_block_value(block, L"PATH");
    g2a_emit_environment_value("PATH", value, FALSE);
}

static void g2a_emit_environment_facts_from_process(void) {
    const wchar_t *names[] = {
        L"USERNAME", L"USERDOMAIN", L"USERPROFILE", L"LOCALAPPDATA",
        L"APPDATA", L"TEMP", L"TMP", L"PATH"
    };
    const char *labels[] = {
        "USERNAME", "USERDOMAIN", "USERPROFILE", "LOCALAPPDATA",
        "APPDATA", "TEMP", "TMP", "PATH"
    };
    const BOOL paths[] = {FALSE, FALSE, TRUE, TRUE, FALSE, TRUE, FALSE, FALSE};
    DWORD index;
    WCHAR value[32768];
    for (index = 0; index < sizeof(names) / sizeof(names[0]); ++index) {
        DWORD length = GetEnvironmentVariableW(names[index], value, sizeof(value) / sizeof(value[0]));
        if (length == 0 || length >= sizeof(value) / sizeof(value[0])) {
            g2a_emit_environment_value(labels[index], NULL, paths[index]);
        } else {
            g2a_emit_environment_value(labels[index], value, paths[index]);
        }
    }
}

static BOOL g2a_prepare_environment(
    HANDLE token,
    BOOL use_user_environment,
    LPVOID *environment
) {
    *environment = NULL;
    if (!use_user_environment) {
        g2a_emit("G2A_ENV_VARIANT=NULL\n");
        g2a_emit("G2A_ENV_CREATE=NOT_USED\n");
        g2a_emit_environment_facts_from_process();
        return TRUE;
    }
    g2a_emit("G2A_ENV_VARIANT=USER_BLOCK\n");
    if (!CreateEnvironmentBlock(environment, token, FALSE)) {
        g2a_u32("G2A_ENV_CREATE_ERROR=", GetLastError());
        g2a_emit("G2A_ENV_CREATE=FAIL\n");
        return FALSE;
    }
    g2a_emit("G2A_ENV_CREATE=PASS\n");
    g2a_emit_environment_facts_from_block((const wchar_t *)*environment);
    return TRUE;
}

static void g2a_destroy_environment(LPVOID environment) {
    if (environment == NULL) {
        g2a_emit("G2A_ENV_DESTROY=NOT_USED\n");
    } else {
        g2a_bool("G2A_ENV_DESTROY=", DestroyEnvironmentBlock(environment));
    }
}

static void g2a_emit_attribute_contract(
    BOOL pty,
    G2A_ATTRIBUTE_STAGE stage
) {
    if (stage == G2A_ATTRIBUTE_STAGE_SECURITY) {
        g2a_emit("G2A_ATTRIBUTES=SECURITY_CAPABILITIES\n");
    } else if (stage == G2A_ATTRIBUTE_STAGE_JOB) {
        g2a_emit("G2A_ATTRIBUTES=SECURITY_CAPABILITIES,JOB_LIST\n");
    } else if (pty) {
        g2a_emit("G2A_ATTRIBUTES=SECURITY_CAPABILITIES,JOB_LIST,PSEUDOCONSOLE\n");
    } else {
        g2a_emit("G2A_ATTRIBUTES=SECURITY_CAPABILITIES,JOB_LIST,HANDLE_LIST\n");
    }
}

static BOOL g2a_update_common_attributes(
    LPPROC_THREAD_ATTRIBUTE_LIST attributes,
    HANDLE job,
    PSID app_sid,
    HANDLE *handles,
    DWORD handle_count,
    HANDLE pseudo_console,
    BOOL pty,
    G2A_ATTRIBUTE_STAGE stage
) {
    SECURITY_CAPABILITIES capabilities;
    ZeroMemory(&capabilities, sizeof(capabilities));
    capabilities.AppContainerSid = app_sid;
    capabilities.Capabilities = NULL;
    capabilities.CapabilityCount = 0;
    capabilities.Reserved = 0;
    if (!UpdateProcThreadAttribute(attributes, 0, G2A_ATTRIBUTE_SECURITY_CAPABILITIES,
        &capabilities, sizeof(capabilities), NULL, NULL)) {
        g2a_u32("G2A_ATTRIBUTE_SECURITY_CAPABILITIES_ERROR=", GetLastError());
        return FALSE;
    }
    if (stage >= G2A_ATTRIBUTE_STAGE_JOB) {
        HANDLE jobs[1] = {job};
        if (!UpdateProcThreadAttribute(attributes, 0, G2A_ATTRIBUTE_JOB_LIST,
            jobs, sizeof(jobs), NULL, NULL)) {
            g2a_u32("G2A_ATTRIBUTE_JOB_LIST_ERROR=", GetLastError());
            return FALSE;
        }
    }
    if (stage < G2A_ATTRIBUTE_STAGE_IO) {
        return TRUE;
    }
    if (pty) {
        if (!UpdateProcThreadAttribute(attributes, 0, G2A_ATTRIBUTE_PSEUDOCONSOLE,
            pseudo_console, sizeof(pseudo_console), NULL, NULL)) {
            g2a_u32("G2A_ATTRIBUTE_PSEUDOCONSOLE_ERROR=", GetLastError());
            return FALSE;
        }
    } else if (!UpdateProcThreadAttribute(attributes, 0, G2A_ATTRIBUTE_HANDLE_LIST,
        handles, sizeof(HANDLE) * handle_count, NULL, NULL)) {
        g2a_u32("G2A_ATTRIBUTE_HANDLE_LIST_ERROR=", GetLastError());
        return FALSE;
    }
    return TRUE;
}

static BOOL g2a_create_child(
    const wchar_t *self,
    const wchar_t *mode,
    const wchar_t *fixture_root,
    const wchar_t *current_directory,
    G2A_PROFILE *profile,
    HANDLE job,
    BOOL pty,
    HANDLE input_read,
    HANDLE output_write,
    HANDLE error_write,
    HANDLE pseudo_console,
    PROCESS_INFORMATION *process,
    BOOL use_user_environment,
    G2A_ATTRIBUTE_STAGE stage,
    BOOL minimal_child,
    G2A_PROCESS_API process_api
) {
    SIZE_T bytes = 0;
    LPPROC_THREAD_ATTRIBUTE_LIST attributes = NULL;
    STARTUPINFOEXW startup;
    HANDLE handles[3] = {input_read, output_write, error_write};
    WCHAR command[32768];
    int count;
    BOOL created = FALSE;
    LPVOID environment = NULL;
    DWORD attribute_count = (DWORD)stage;
    count = swprintf_s(command, sizeof(command) / sizeof(command[0]),
        L"\"%s\" child-%s%s \"%s\"", self, mode,
        minimal_child ? L"-minimal" : L"", fixture_root);
    if (count <= 0) {
        g2a_emit("G2A_CHILD_CREATE=FAIL\n");
        return FALSE;
    }
    g2a_emit_attribute_contract(pty, stage);
    g2a_emitf(
        "G2A_PROCESS_API=%s\n",
        process_api == G2A_PROCESS_API_CURRENT ? "CreateProcessW" : "CreateProcessAsUserW"
    );
    g2a_emit_wide("G2A_LP_APPLICATION_NAME=", self);
    g2a_emit_wide("G2A_COMMAND_EXECUTABLE=", self);
    g2a_emit_wide("G2A_COMMAND_LINE=", command);
    g2a_emit_wide("G2A_LP_CURRENT_DIRECTORY=", current_directory);
    g2a_emitf("G2A_INHERIT_HANDLES=%s\n", !pty ? "TRUE" : "FALSE");
    g2a_emit("G2A_CREATION_FLAGS=CREATE_UNICODE_ENVIRONMENT,CREATE_NO_WINDOW,EXTENDED_STARTUPINFO_PRESENT\n");
    (void)InitializeProcThreadAttributeList(NULL, attribute_count, 0, &bytes);
    if (bytes == 0) {
        g2a_u32("G2A_ATTRIBUTE_LIST_ERROR=", GetLastError());
        g2a_u32("G2A_CHILD_CREATE_ERROR=", GetLastError());
        return FALSE;
    }
    attributes = (LPPROC_THREAD_ATTRIBUTE_LIST)HeapAlloc(GetProcessHeap(), 0, bytes);
    if (attributes == NULL || !InitializeProcThreadAttributeList(attributes, attribute_count, 0, &bytes) ||
        !g2a_update_common_attributes(attributes, job, profile->Sid, handles, 3, pseudo_console, pty, stage)) {
        g2a_u32("G2A_ATTRIBUTE_LIST_ERROR=", GetLastError());
        g2a_u32("G2A_CHILD_CREATE_ERROR=", GetLastError());
        if (attributes != NULL) HeapFree(GetProcessHeap(), 0, attributes);
        return FALSE;
    }
    ZeroMemory(&startup, sizeof(startup));
    startup.StartupInfo.cb = sizeof(startup);
    if (!pty) {
        startup.StartupInfo.dwFlags = G2A_STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = input_read;
        startup.StartupInfo.hStdOutput = output_write;
        startup.StartupInfo.hStdError = error_write;
    }
    startup.lpAttributeList = attributes;
    ZeroMemory(process, sizeof(*process));
    {
        HANDLE token = NULL;
        BOOL token_ok = OpenProcessToken(GetCurrentProcess(),
            TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT | TOKEN_ADJUST_SESSIONID,
            &token);
        if (!token_ok) {
            g2a_u32("G2A_SOURCE_TOKEN_ERROR=", GetLastError());
        } else {
            if (!g2a_prepare_environment(token, use_user_environment, &environment)) {
                g2a_emit("G2A_CHILD_CREATE=FAIL\n");
                g2a_u32("G2A_CHILD_CREATE_ERROR=", GetLastError());
                CloseHandle(token);
                DeleteProcThreadAttributeList(attributes);
                HeapFree(GetProcessHeap(), 0, attributes);
                return FALSE;
            }
            g2a_emit("G2A_CREATEPROCESS_CALL=ABOUT_TO_CALL\n");
            if (process_api == G2A_PROCESS_API_CURRENT) {
                created = CreateProcessW(
                    self,
                    command,
                    NULL,
                    NULL,
                    !pty,
                    G2A_CREATE_UNICODE_ENVIRONMENT | G2A_CREATE_NO_WINDOW |
                        G2A_EXTENDED_STARTUPINFO_PRESENT,
                    environment,
                    current_directory,
                    &startup.StartupInfo,
                    process
                );
            } else {
                created = CreateProcessAsUserW(
                    token,
                    self,
                    command,
                    NULL,
                    NULL,
                    !pty,
                    G2A_CREATE_UNICODE_ENVIRONMENT | G2A_CREATE_NO_WINDOW |
                        G2A_EXTENDED_STARTUPINFO_PRESENT,
                    environment,
                    current_directory,
                    &startup.StartupInfo,
                    process
                );
            }
            if (!created) {
                g2a_u32("G2A_CREATEPROCESS_ERROR=", GetLastError());
                g2a_u32("G2A_CHILD_CREATE_ERROR=", GetLastError());
            }
            g2a_destroy_environment(environment);
            CloseHandle(token);
        }
    }
    DeleteProcThreadAttributeList(attributes);
    HeapFree(GetProcessHeap(), 0, attributes);
    g2a_bool("G2A_CHILD_CREATE=", created);
    if (created) {
        g2a_u32("G2A_CHILD_PID=", process->dwProcessId);
    }
    return created;
}

static void g2a_capture_descendant_pid(const char *text);

static void g2a_parse_variant(
    const wchar_t *mode,
    BOOL *pty,
    BOOL *use_user_environment,
    G2A_ATTRIBUTE_STAGE *stage,
    G2A_PROCESS_API *process_api
) {
    *pty = wcsncmp(mode, L"pty", 3) == 0;
    *use_user_environment =
        wcsstr(mode, L"-env-user") != NULL || wcsstr(mode, L"-user") != NULL ||
        wcsstr(mode, L"cd-") != NULL || wcsstr(mode, L"api-") != NULL;
    *process_api = wcsstr(mode, L"api-current") != NULL
        ? G2A_PROCESS_API_CURRENT
        : G2A_PROCESS_API_AS_USER;
    if (wcsstr(mode, L"-a0") != NULL) {
        *stage = G2A_ATTRIBUTE_STAGE_SECURITY;
    } else if (wcsstr(mode, L"-a1") != NULL) {
        *stage = G2A_ATTRIBUTE_STAGE_JOB;
    } else if (wcsstr(mode, L"-a2") != NULL) {
        *stage = G2A_ATTRIBUTE_STAGE_IO;
    } else if (wcsstr(mode, L"cd-") != NULL || wcsstr(mode, L"api-") != NULL) {
        *stage = G2A_ATTRIBUTE_STAGE_SECURITY;
    } else {
        *stage = G2A_ATTRIBUTE_STAGE_IO;
    }
}

static const wchar_t *g2a_current_directory_for_mode(
    const wchar_t *mode,
    const wchar_t *workspace,
    const wchar_t *fixture_root
) {
    if (wcsstr(mode, L"cd-null") != NULL || wcsstr(mode, L"api-null") != NULL ||
        wcsstr(mode, L"layer-null") != NULL) {
        return NULL;
    }
    if (wcsstr(mode, L"workspace") != NULL) {
        return workspace;
    }
    return fixture_root;
}

static void g2a_forward_pipe(HANDLE read_handle) {
    char buffer[G2A_PIPE_BUFFER];
    DWORD available = 0;
    DWORD received = 0;
    while (PeekNamedPipe(read_handle, NULL, 0, NULL, &available, NULL) && available > 0) {
        DWORD amount = available < sizeof(buffer) ? available : sizeof(buffer);
        if (!ReadFile(read_handle, buffer, amount, &received, NULL) || received == 0) {
            break;
        }
        {
            char bounded[G2A_PIPE_BUFFER + 1];
            memcpy(bounded, buffer, received);
            bounded[received] = '\0';
            g2a_capture_descendant_pid(bounded);
            g2a_emit(bounded);
        }
    }
}

static BOOL g2a_pid_active(DWORD pid, HANDLE *process) {
    DWORD code = 0;
    *process = OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!g2a_valid_handle(*process)) {
        return FALSE;
    }
    if (!GetExitCodeProcess(*process, &code)) {
        CloseHandle(*process);
        *process = NULL;
        return FALSE;
    }
    return code == G2A_STILL_ACTIVE;
}

static DWORD g2a_seen_descendant_pid = 0;

static void g2a_capture_descendant_pid(const char *text) {
    const char *marker = "G2A_DESCENDANT_PID=";
    const char *position = strstr(text, marker);
    if (position != NULL) {
        unsigned long value = strtoul(position + strlen(marker), NULL, 10);
        if (value > 0 && value <= 0xFFFFFFFFUL) {
            g2a_seen_descendant_pid = (DWORD)value;
        }
    }
}

static int g2a_controller(const wchar_t *mode, const wchar_t *workspace, const wchar_t *fixture_root) {
    G2A_PROFILE profile;
    HANDLE job = NULL;
    HANDLE input_read = NULL, input_write = NULL;
    HANDLE output_read = NULL, output_write = NULL;
    HANDLE error_read = NULL, error_write = NULL;
    HANDLE pty_input_read = NULL, pty_input_write = NULL;
    HANDLE pty_output_read = NULL, pty_output_write = NULL;
    HANDLE pseudo_console = NULL;
    PROCESS_INFORMATION process;
    HANDLE descendant_handle = NULL;
    WCHAR self[MAX_PATH * 4];
    BOOL pty = FALSE;
    BOOL use_user_environment = FALSE;
    G2A_ATTRIBUTE_STAGE stage = G2A_ATTRIBUTE_STAGE_IO;
    G2A_PROCESS_API process_api = G2A_PROCESS_API_AS_USER;
    const wchar_t *current_directory = fixture_root;
    DWORD wait_result;
    int result = 0;
    g2a_parse_variant(mode, &pty, &use_user_environment, &stage, &process_api);
    current_directory = g2a_current_directory_for_mode(mode, workspace, fixture_root);
    ZeroMemory(&profile, sizeof(profile));
    ZeroMemory(&process, sizeof(process));
    if (!g2a_profile_create(&profile)) {
        return 20;
    }
    if (GetModuleFileNameW(NULL, self, sizeof(self) / sizeof(self[0])) == 0) {
        g2a_u32("G2A_SELF_PATH_ERROR=", GetLastError());
        g2a_profile_cleanup(&profile);
        return 21;
    }
    (void)g2a_grant_sid(workspace, profile.Sid, G2A_FILE_ALL_TEMP);
    (void)g2a_grant_sid(self, profile.Sid, G2A_FILE_GENERIC_READ | FILE_EXECUTE);
    if (!g2a_prepare_fixtures(fixture_root, profile.Sid)) {
        g2a_emit("G2A_FIXTURES=INCONCLUSIVE\n");
    } else {
        g2a_emit("G2A_FIXTURES=READY\n");
    }
    g2a_emit_wide("G2A_CURRENT_DIRECTORY_VARIANT=", current_directory);
    g2a_path_attestation(self, workspace, fixture_root, current_directory);
    if (!g2a_make_job(&job)) {
        g2a_u32("G2A_JOB_CREATE_ERROR=", GetLastError());
        g2a_profile_cleanup(&profile);
        return 22;
    }
    if (pty) {
        COORD size;
        size.X = 80;
        size.Y = 25;
        if (!g2a_make_pipe(&pty_input_read, &pty_input_write) ||
            !g2a_make_pipe(&pty_output_read, &pty_output_write)) {
            g2a_u32("G2A_PTY_CREATE_ERROR=", GetLastError());
            result = 23;
            goto cleanup;
        }
        (void)g2a_noninherit(pty_input_write);
        (void)g2a_noninherit(pty_output_read);
        {
            HRESULT hr = CreatePseudoConsole(size, pty_input_read, pty_output_write, 0, &pseudo_console);
            if (FAILED(hr)) {
            g2a_i32("G2A_PTY_CREATE_HRESULT=", hr);
            g2a_emit("G2A_PTY_CREATE=FAIL\n");
            result = 24;
            goto cleanup;
            }
        }
        g2a_emit("G2A_PTY_CREATE=PASS\n");
        if (!g2a_create_child(self, L"pty", fixture_root, current_directory, &profile, job, TRUE,
            NULL, NULL, NULL, pseudo_console, &process, use_user_environment, stage,
            stage < G2A_ATTRIBUTE_STAGE_JOB, process_api)) {
            result = 25;
            goto cleanup;
        }
        if (pty_input_write != NULL) {
            const char input[] = "g2a-input\n";
            DWORD written = 0;
            (void)WriteFile(pty_input_write, input, (DWORD)strlen(input), &written, NULL);
        }
    } else {
        if (!g2a_make_pipe(&input_read, &input_write) || !g2a_make_pipe(&output_read, &output_write) ||
            !g2a_make_pipe(&error_read, &error_write)) {
            g2a_u32("G2A_PIPE_CREATE_ERROR=", GetLastError());
            result = 26;
            goto cleanup;
        }
        (void)g2a_noninherit(input_write);
        (void)g2a_noninherit(output_read);
        (void)g2a_noninherit(error_read);
        g2a_emit("G2A_PIPE_CREATE=PASS\n");
        if (!g2a_create_child(self, L"pipe", fixture_root, current_directory, &profile, job, FALSE,
            input_read, output_write, error_write, NULL, &process, use_user_environment, stage,
            stage < G2A_ATTRIBUTE_STAGE_JOB, process_api)) {
            result = 27;
            goto cleanup;
        }
        {
            const char input[] = "g2a-input\n";
            DWORD written = 0;
            (void)WriteFile(input_write, input, (DWORD)strlen(input), &written, NULL);
        }
    }
    {
        BOOL in_job = FALSE;
        if (IsProcessInJob(process.hProcess, job, &in_job)) {
            g2a_bool("G2A_JOB_MEMBER=", in_job);
        } else {
            g2a_u32("G2A_JOB_MEMBER_ERROR=", GetLastError());
        }
    }
    for (;;) {
        g2a_forward_pipe(pty ? pty_output_read : output_read);
        wait_result = WaitForSingleObject(process.hProcess, 20);
        if (wait_result != WAIT_TIMEOUT) {
            break;
        }
    }
    g2a_forward_pipe(pty ? pty_output_read : output_read);
    if (g2a_seen_descendant_pid != 0) {
        DWORD retries = 0;
        while (retries < 50 && !g2a_pid_active(g2a_seen_descendant_pid, &descendant_handle)) {
            Sleep(10);
            retries++;
        }
        g2a_bool("G2A_DESCENDANT_ACTIVE_BEFORE_CLOSE=", descendant_handle != NULL);
    } else {
        g2a_emit("G2A_DESCENDANT_ACTIVE_BEFORE_CLOSE=NOT_OBSERVED\n");
    }
    if (wait_result == WAIT_OBJECT_0) {
        DWORD exit_code = 0;
        (void)GetExitCodeProcess(process.hProcess, &exit_code);
        g2a_u32("G2A_CHILD_EXIT=", exit_code);
    } else {
        g2a_u32("G2A_CHILD_WAIT_ERROR=", GetLastError());
        result = 28;
    }
    g2a_emit("G2A_SCOPE_COMPLETE=PASS\n");
cleanup:
    if (process.hProcess != NULL) CloseHandle(process.hProcess);
    if (process.hThread != NULL) CloseHandle(process.hThread);
    if (pseudo_console != NULL) ClosePseudoConsole(pseudo_console);
    if (pty_input_read != NULL) CloseHandle(pty_input_read);
    if (pty_input_write != NULL) CloseHandle(pty_input_write);
    if (pty_output_read != NULL) CloseHandle(pty_output_read);
    if (pty_output_write != NULL) CloseHandle(pty_output_write);
    if (input_read != NULL) CloseHandle(input_read);
    if (input_write != NULL) CloseHandle(input_write);
    if (output_read != NULL) CloseHandle(output_read);
    if (output_write != NULL) CloseHandle(output_write);
    if (error_read != NULL) CloseHandle(error_read);
    if (error_write != NULL) CloseHandle(error_write);
    if (job != NULL) {
        CloseHandle(job);
        g2a_emit("G2A_JOB_CLOSE=PASS\n");
    }
    if (descendant_handle != NULL) {
        DWORD descendant_wait = WaitForSingleObject(descendant_handle, 5000);
        g2a_bool("G2A_DESCENDANT_REAPED=", descendant_wait == WAIT_OBJECT_0);
        CloseHandle(descendant_handle);
    } else {
        g2a_emit("G2A_DESCENDANT_REAPED=NOT_OBSERVED\n");
    }
    g2a_profile_cleanup(&profile);
    return result;
}

int wmain(int argc, wchar_t **argv) {
    if (argc >= 2 && wcscmp(argv[1], L"descendant") == 0) {
        return g2a_descendant(NULL);
    }
    if (argc >= 3 && wcscmp(argv[1], L"child-pipe") == 0) {
        g2a_child_pipe(argv[2], FALSE);
        return 0;
    }
    if (argc >= 3 && wcscmp(argv[1], L"child-pipe-minimal") == 0) {
        g2a_child_pipe(argv[2], TRUE);
        return 0;
    }
    if (argc >= 3 && wcscmp(argv[1], L"child-pty") == 0) {
        g2a_child_pty(argv[2], FALSE);
        return 0;
    }
    if (argc >= 3 && wcscmp(argv[1], L"child-pty-minimal") == 0) {
        g2a_child_pty(argv[2], TRUE);
        return 0;
    }
    if (argc >= 4 && (wcsncmp(argv[1], L"pipe", 4) == 0 || wcsncmp(argv[1], L"pty", 3) == 0)) {
        return g2a_controller(argv[1], argv[2], argv[3]);
    }
    g2a_emit("G2A_INVALID_ARGUMENTS\n");
    return 30;
}
