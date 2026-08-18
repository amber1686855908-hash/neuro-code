#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00
#endif

/*
 * W5 Gate 2A is evidence-only.  This helper is intentionally self-contained:
 * it creates one disposable AppContainer profile, launches bounded A0/A1/A2
 * evidence children with the canonical attribute sets, and reports bounded
 * facts only.  It never changes a system ACL or policy.  ACL changes are
 * limited to disposable fixture paths.
 */
#define WIN32_LEAN_AND_MEAN

#include <windows.h>
#include <aclapi.h>
#include <ConsoleApi2.h>
#include <processthreadsapi.h>
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

/* Keep the evidence probe tied to the Windows SDK contract.  The fallbacks
 * only keep older SDK headers buildable; the compile-time checks below make a
 * changed encoding fail the probe build instead of silently testing another
 * attribute. */
#ifndef PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES
#define PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES 0x00020009
#endif
#ifndef PROC_THREAD_ATTRIBUTE_HANDLE_LIST
#define PROC_THREAD_ATTRIBUTE_HANDLE_LIST 0x00020002
#endif
#ifndef PROC_THREAD_ATTRIBUTE_JOB_LIST
#define PROC_THREAD_ATTRIBUTE_JOB_LIST 0x0002000D
#endif
#ifndef PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE
#define PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE 0x00020016
#endif
/* The SDK definitions use ProcThreadAttributeValue(), which contains a cast
 * and therefore cannot be evaluated by the preprocessor's #if expression.
 * Keep the equality checks in C so the compiler still rejects an SDK whose
 * encodings drift from the documented values. */
typedef char g2a_security_attribute_encoding_check[
    (PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES == 0x00020009) ? 1 : -1
];
typedef char g2a_handle_list_attribute_encoding_check[
    (PROC_THREAD_ATTRIBUTE_HANDLE_LIST == 0x00020002) ? 1 : -1
];
typedef char g2a_job_list_attribute_encoding_check[
    (PROC_THREAD_ATTRIBUTE_JOB_LIST == 0x0002000D) ? 1 : -1
];
typedef char g2a_pseudoconsole_attribute_encoding_check[
    (PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE == 0x00020016) ? 1 : -1
];
#define G2A_ATTRIBUTE_SECURITY_CAPABILITIES PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES
#define G2A_ATTRIBUTE_HANDLE_LIST PROC_THREAD_ATTRIBUTE_HANDLE_LIST
#define G2A_ATTRIBUTE_JOB_LIST PROC_THREAD_ATTRIBUTE_JOB_LIST
#define G2A_ATTRIBUTE_PSEUDOCONSOLE PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE
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

typedef enum _G2A_TRANSPORT {
    G2A_TRANSPORT_NONE = 0,
    G2A_TRANSPORT_PIPE = 1,
    G2A_TRANSPORT_PTY = 2
} G2A_TRANSPORT;

/*
 * UpdateProcThreadAttribute does not copy lpValue.  Every pointer-valued
 * attribute below therefore points into this context, which remains alive
 * until DeleteProcThreadAttributeList after CreateProcess*.  The
 * pseudoconsole contract takes the HPCON value itself (not &HPCON); retaining
 * the value here keeps the owned handle visible for the same lifetime.
 */
typedef struct _G2A_ATTRIBUTE_BACKING {
    SECURITY_CAPABILITIES security_capabilities;
    HANDLE job_handles[1];
    HANDLE io_handles[3];
    HPCON pseudo_console;
} G2A_ATTRIBUTE_BACKING;

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

static void g2a_emit_privileges_with_prefix(HANDLE token, const char *prefix) {
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    TOKEN_PRIVILEGES *privileges;
    DWORD index;
    DWORD enabled = 0;
    DWORD unexpected = 0;
    if (!g2a_query_token(token, G2A_TOKEN_PRIVILEGES, &buffer, &size, &error)) {
        g2a_emitf("%sPRIVILEGE_QUERY_ERROR=%lu\n", prefix, (unsigned long)error);
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
    g2a_emitf("%sPRIVILEGE_COUNT=%lu\n", prefix, (unsigned long)privileges->PrivilegeCount);
    g2a_emitf("%sENABLED_PRIVILEGE_COUNT=%lu\n", prefix, (unsigned long)enabled);
    g2a_emitf("%sUNEXPECTED_ENABLED_PRIVILEGES=%lu\n", prefix, (unsigned long)unexpected);
    HeapFree(GetProcessHeap(), 0, buffer);
}

static void g2a_emit_privileges(HANDLE token) {
    g2a_emit_privileges_with_prefix(token, "G2A_TOKEN_");
}

static void g2a_emit_integrity_with_prefix(HANDLE token, const char *prefix) {
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    TOKEN_MANDATORY_LABEL *label;
    BYTE count;
    DWORD rid;
    if (!g2a_query_token(token, G2A_TOKEN_INTEGRITY_LEVEL, &buffer, &size, &error)) {
        g2a_emitf("%sINTEGRITY_ERROR=%lu\n", prefix, (unsigned long)error);
        return;
    }
    (void)size;
    label = (TOKEN_MANDATORY_LABEL *)buffer;
    count = *GetSidSubAuthorityCount(label->Label.Sid);
    rid = count == 0 ? 0 : *GetSidSubAuthority(label->Label.Sid, count - 1);
    g2a_emitf("%sINTEGRITY_RID=%lu\n", prefix, (unsigned long)rid);
    HeapFree(GetProcessHeap(), 0, buffer);
}

static void g2a_emit_integrity(HANDLE token) {
    g2a_emit_integrity_with_prefix(token, "G2A_TOKEN_");
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

static void g2a_emit_mandatory_policy_with_prefix(HANDLE token, const char *prefix) {
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    if (g2a_query_token(token, G2A_TOKEN_MANDATORY_POLICY, &buffer, &size, &error)) {
        g2a_emitf("%sMANDATORY_POLICY=0x%08lX\n", prefix,
            (unsigned long)((TOKEN_MANDATORY_POLICY *)buffer)->Policy);
        HeapFree(GetProcessHeap(), 0, buffer);
    } else {
        g2a_emitf("%sMANDATORY_POLICY_ERROR=%lu\n", prefix, (unsigned long)error);
    }
}

static void g2a_attest_external_token(HANDLE process, PSID expected_sid) {
    HANDLE token = NULL;
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    BOOL is_app = FALSE;
    BOOL sid_match = FALSE;
    const char *prefix = "G2A_EXTERNAL_TOKEN_";
    if (!OpenProcessToken(process, TOKEN_QUERY, &token)) {
        g2a_u32("G2A_EXTERNAL_TOKEN_OPEN_ERROR=", GetLastError());
        return;
    }
    g2a_emit_token_sid(token, G2A_TOKEN_USER, "G2A_EXTERNAL_TOKEN_USER=");
    if (g2a_query_token(token, G2A_TOKEN_IS_APP_CONTAINER, &buffer, &size, &error)) {
        is_app = *(BOOL *)buffer;
        g2a_bool("G2A_EXTERNAL_TOKEN_IS_APP_CONTAINER=", is_app);
        HeapFree(GetProcessHeap(), 0, buffer);
    } else {
        g2a_u32("G2A_EXTERNAL_TOKEN_IS_APP_CONTAINER_ERROR=", error);
    }
    if (g2a_query_token(token, G2A_TOKEN_APP_CONTAINER_SID, &buffer, &size, &error)) {
        TOKEN_APPCONTAINER_INFORMATION *info = (TOKEN_APPCONTAINER_INFORMATION *)buffer;
        if (info->TokenAppContainer != NULL) {
            sid_match = expected_sid != NULL && EqualSid(info->TokenAppContainer, expected_sid);
            g2a_sid("G2A_EXTERNAL_TOKEN_APPCONTAINER_SID=", info->TokenAppContainer);
        } else {
            g2a_emit("G2A_EXTERNAL_TOKEN_APPCONTAINER_SID=UNAVAILABLE\n");
        }
        HeapFree(GetProcessHeap(), 0, buffer);
    } else {
        g2a_u32("G2A_EXTERNAL_TOKEN_APPCONTAINER_SID_ERROR=", error);
    }
    g2a_bool("G2A_EXTERNAL_TOKEN_SID_MATCH=", sid_match);
    g2a_emit_groups(token, G2A_TOKEN_CAPABILITIES, "G2A_EXTERNAL_TOKEN_CAPABILITY_");
    g2a_emit_groups(token, G2A_TOKEN_RESTRICTED_SIDS, "G2A_EXTERNAL_TOKEN_RESTRICTED_");
    g2a_emit_groups(token, G2A_TOKEN_GROUPS, "G2A_EXTERNAL_TOKEN_GROUP_");
    g2a_emit_privileges_with_prefix(token, prefix);
    g2a_emit_integrity_with_prefix(token, prefix);
    g2a_emit_mandatory_policy_with_prefix(token, prefix);
    g2a_bool("G2A_EXTERNAL_TOKEN_QUERY_CLOSED=", CloseHandle(token));
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

static void g2a_emit_wide(const char *prefix, const wchar_t *value);

static void g2a_descendant_policy(void) {
    PROCESS_MITIGATION_CHILD_PROCESS_POLICY policy;
    ZeroMemory(&policy, sizeof(policy));
    if (!GetProcessMitigationPolicy(
        GetCurrentProcess(), ProcessChildProcessPolicy, &policy, sizeof(policy)
    )) {
        g2a_bool("G2A_DESCENDANT_POLICY_AVAILABLE=", FALSE);
        g2a_u32("G2A_DESCENDANT_POLICY_ERROR=", GetLastError());
        return;
    }
    g2a_bool("G2A_DESCENDANT_POLICY_AVAILABLE=", TRUE);
    g2a_bool("G2A_DESCENDANT_NO_CHILD_PROCESS_CREATION=", policy.NoChildProcessCreation);
    g2a_bool("G2A_DESCENDANT_AUDIT_NO_CHILD_PROCESS_CREATION=", policy.AuditNoChildProcessCreation);
}

static void g2a_descendant_launch_inputs(const wchar_t *workspace) {
    WCHAR self[MAX_PATH * 4];
    WCHAR current[MAX_PATH * 4];
    DWORD length;
    HANDLE readable;
    DWORD attributes;
    length = GetModuleFileNameW(NULL, self, sizeof(self) / sizeof(self[0]));
    g2a_bool("G2A_DESCENDANT_SELF_PATH_AVAILABLE=", length > 0 && length < sizeof(self) / sizeof(self[0]));
    if (length > 0 && length < sizeof(self) / sizeof(self[0])) {
        attributes = GetFileAttributesW(self);
        g2a_bool("G2A_DESCENDANT_SELF_EXISTS=", attributes != INVALID_FILE_ATTRIBUTES);
        readable = CreateFileW(
            self, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL
        );
        g2a_bool("G2A_DESCENDANT_SELF_READABLE=", g2a_valid_handle(readable));
        if (g2a_valid_handle(readable)) {
            CloseHandle(readable);
        }
        g2a_emit_wide("G2A_DESCENDANT_SELF_PATH=", self);
    }
    length = GetCurrentDirectoryW(sizeof(current) / sizeof(current[0]), current);
    g2a_bool("G2A_DESCENDANT_CURRENT_DIRECTORY_AVAILABLE=", length > 0 && length < sizeof(current) / sizeof(current[0]));
    if (length > 0 && length < sizeof(current) / sizeof(current[0])) {
        g2a_emit_wide("G2A_DESCENDANT_CURRENT_DIRECTORY=", current);
    }
    if (workspace == NULL || workspace[0] == L'\0') {
        g2a_emit("G2A_DESCENDANT_WORKSPACE=UNAVAILABLE\n");
    } else {
        attributes = GetFileAttributesW(workspace);
        g2a_bool("G2A_DESCENDANT_WORKSPACE_EXISTS=", attributes != INVALID_FILE_ATTRIBUTES);
        g2a_emit_wide("G2A_DESCENDANT_WORKSPACE=", workspace);
    }
    {
        LPWCH environment = GetEnvironmentStringsW();
        g2a_bool("G2A_DESCENDANT_ENV_PRESENT=", environment != NULL);
        if (environment != NULL) {
            (void)FreeEnvironmentStringsW(environment);
        }
    }
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

static BOOL g2a_spawn_descendant(const wchar_t *variant, const wchar_t *workspace) {
    WCHAR self[MAX_PATH * 4];
    WCHAR command[MAX_PATH * 4 + 64];
    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    LPWCH environment = NULL;
    LPCWSTR application_name = NULL;
    LPCWSTR current_directory = NULL;
    BOOL use_environment = variant != NULL && wcsstr(variant, L"env") != NULL;
    DWORD length = GetModuleFileNameW(NULL, self, sizeof(self) / sizeof(self[0]));
    if (length == 0 || length >= sizeof(self) / sizeof(self[0])) {
        g2a_u32("G2A_DESCENDANT_CREATE_ERROR=", GetLastError());
        return FALSE;
    }
    if (swprintf_s(command, sizeof(command) / sizeof(command[0]), L"\"%s\" descendant", self) < 0) {
        g2a_emit("G2A_DESCENDANT_CREATE_ERROR=87\n");
        return FALSE;
    }
    if (variant != NULL && wcsstr(variant, L"application") != NULL) {
        application_name = self;
    }
    if (variant != NULL && (wcsstr(variant, L"cwd") != NULL || wcsstr(variant, L"env") != NULL)) {
        current_directory = workspace;
    }
    if (use_environment) {
        environment = GetEnvironmentStringsW();
        if (environment == NULL) {
            g2a_u32("G2A_DESCENDANT_CREATE_ERROR=", GetLastError());
            return FALSE;
        }
    }
    ZeroMemory(&startup, sizeof(startup));
    ZeroMemory(&process, sizeof(process));
    startup.cb = sizeof(startup);
    if (!CreateProcessW(application_name, command, NULL, NULL, FALSE,
        G2A_CREATE_UNICODE_ENVIRONMENT | G2A_CREATE_NO_WINDOW,
        use_environment ? environment : NULL, current_directory, &startup, &process)) {
        g2a_u32("G2A_DESCENDANT_CREATE_ERROR=", GetLastError());
        if (environment != NULL) {
            (void)FreeEnvironmentStringsW(environment);
        }
        return FALSE;
    }
    if (environment != NULL) {
        (void)FreeEnvironmentStringsW(environment);
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

static void g2a_child_common(
    const wchar_t *fixture_root,
    HANDLE input,
    BOOL pty,
    BOOL minimal,
    BOOL input_only,
    BOOL no_descendant
) {
    char line[128];
    g2a_emit("G2A_CHILD_STARTED\n");
    g2a_attest_token();
    if (minimal) {
        g2a_emit("G2A_CHILD_MINIMAL=TRUE\n");
        g2a_emit("G2A_CHILD_FINISHED\n");
        return;
    }
    if (input_only) {
        if (input != NULL && g2a_read_line(input, line, sizeof(line))) {
            g2a_emitf("G2A_STDIN=%s\n", strcmp(line, "g2a-input") == 0 ? "PASS" : "UNEXPECTED");
        } else {
            g2a_emit("G2A_STDIN=UNAVAILABLE\n");
        }
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
    if (!no_descendant) {
        (void)g2a_spawn_descendant(L"original", NULL);
    }
    g2a_emit("G2A_CHILD_FINISHED\n");
}

static int g2a_child_pipe(
    const wchar_t *fixture_root,
    BOOL minimal,
    BOOL input_only,
    BOOL no_descendant
) {
    HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    g2a_child_common(fixture_root, input, FALSE, minimal, input_only, no_descendant);
    return 0;
}

static int g2a_child_pty(
    const wchar_t *fixture_root,
    BOOL minimal,
    BOOL input_only,
    BOOL no_descendant
) {
    HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    g2a_child_common(fixture_root, input, TRUE, minimal, input_only, no_descendant);
    return 0;
}

static int g2a_child_descendant(const wchar_t *fixture_root, const wchar_t *workspace, const wchar_t *variant) {
    g2a_emit("G2A_CHILD_STARTED\n");
    g2a_attest_token();
    g2a_descendant_policy();
    g2a_descendant_launch_inputs(workspace);
    (void)g2a_spawn_descendant(variant, workspace);
    g2a_emit("G2A_CHILD_FINISHED\n");
    (void)fixture_root;
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

static BOOL g2a_validate_job(HANDLE job, const char *phase) {
    DWORD handle_flags = 0;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
    DWORD returned = 0;
    BOOL handle_ok;
    BOOL limits_ok;
    BOOL kill_on_close;
    if (!g2a_valid_handle(job)) {
        g2a_emitf("G2A_JOB_%s_VALID=NOT_REQUIRED\n", phase);
        g2a_emitf("G2A_JOB_%s_LIMITS=NOT_REQUIRED\n", phase);
        g2a_emitf("G2A_JOB_%s_KILL_ON_CLOSE=NOT_REQUIRED\n", phase);
        return TRUE;
    }
    SetLastError(ERROR_SUCCESS);
    handle_ok = GetHandleInformation(job, &handle_flags);
    g2a_emitf("G2A_JOB_%s_VALID=%s\n", phase, handle_ok ? "PASS" : "FAIL");
    if (handle_ok) {
        g2a_emitf("G2A_JOB_%s_HANDLE_FLAGS=0x%08lX\n", phase,
            (unsigned long)handle_flags);
    } else {
        g2a_emitf("G2A_JOB_%s_HANDLE_ERROR=%lu\n", phase,
            (unsigned long)GetLastError());
    }
    ZeroMemory(&limits, sizeof(limits));
    SetLastError(ERROR_SUCCESS);
    limits_ok = QueryInformationJobObject(
        job, JobObjectExtendedLimitInformation, &limits, sizeof(limits), &returned
    );
    g2a_emitf("G2A_JOB_%s_LIMITS=%s\n", phase, limits_ok ? "PASS" : "FAIL");
    if (limits_ok) {
        g2a_emitf("G2A_JOB_%s_LIMIT_FLAGS=0x%08lX\n", phase,
            (unsigned long)limits.BasicLimitInformation.LimitFlags);
    } else {
        g2a_emitf("G2A_JOB_%s_LIMIT_ERROR=%lu\n", phase,
            (unsigned long)GetLastError());
    }
    kill_on_close = limits_ok &&
        (limits.BasicLimitInformation.LimitFlags & G2A_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) != 0;
    g2a_emitf("G2A_JOB_%s_KILL_ON_CLOSE=%s\n", phase,
        kill_on_close ? "PASS" : "FAIL");
    return handle_ok && limits_ok && kill_on_close;
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
    BOOL include_security,
    BOOL pty,
    G2A_ATTRIBUTE_STAGE stage
) {
    if (include_security && stage == G2A_ATTRIBUTE_STAGE_SECURITY) {
        g2a_emit("G2A_ATTRIBUTES=SECURITY_CAPABILITIES\n");
    } else if (!include_security && stage == G2A_ATTRIBUTE_STAGE_JOB) {
        g2a_emit("G2A_ATTRIBUTES=JOB_LIST\n");
    } else if (!include_security && stage >= G2A_ATTRIBUTE_STAGE_IO && pty) {
        g2a_emit("G2A_ATTRIBUTES=JOB_LIST,PSEUDOCONSOLE\n");
    } else if (!include_security && stage >= G2A_ATTRIBUTE_STAGE_IO) {
        g2a_emit("G2A_ATTRIBUTES=JOB_LIST,HANDLE_LIST\n");
    } else if (stage == G2A_ATTRIBUTE_STAGE_JOB) {
        g2a_emit("G2A_ATTRIBUTES=SECURITY_CAPABILITIES,JOB_LIST\n");
    } else if (pty) {
        g2a_emit("G2A_ATTRIBUTES=SECURITY_CAPABILITIES,JOB_LIST,PSEUDOCONSOLE\n");
    } else {
        g2a_emit("G2A_ATTRIBUTES=SECURITY_CAPABILITIES,JOB_LIST,HANDLE_LIST\n");
    }
}

static BOOL g2a_update_attribute(
    LPPROC_THREAD_ATTRIBUTE_LIST attributes,
    DWORD_PTR attribute,
    const char *name,
    void *value,
    SIZE_T value_size
) {
    BOOL ok = UpdateProcThreadAttribute(
        attributes, 0, attribute, value, value_size, NULL, NULL
    );
    if (ok) {
        g2a_emitf("G2A_ATTRIBUTE_%s=PASS|CBSIZE=%llu\n", name,
            (unsigned long long)value_size);
    } else {
        g2a_emitf("G2A_ATTRIBUTE_%s=FAIL|ERROR=%lu|CBSIZE=%llu\n", name,
            (unsigned long)GetLastError(), (unsigned long long)value_size);
    }
    return ok;
}

static BOOL g2a_update_common_attributes(
    LPPROC_THREAD_ATTRIBUTE_LIST attributes,
    G2A_ATTRIBUTE_BACKING *backing,
    BOOL include_security,
    G2A_ATTRIBUTE_STAGE stage,
    BOOL pty
) {
    if (include_security && !g2a_update_attribute(
        attributes, G2A_ATTRIBUTE_SECURITY_CAPABILITIES, "SECURITY_CAPABILITIES",
        &backing->security_capabilities, sizeof(backing->security_capabilities))) {
        return FALSE;
    }
    if (stage >= G2A_ATTRIBUTE_STAGE_JOB && !g2a_update_attribute(
        attributes, G2A_ATTRIBUTE_JOB_LIST, "JOB_LIST", backing->job_handles,
        sizeof(backing->job_handles))) {
        return FALSE;
    }
    if (stage < G2A_ATTRIBUTE_STAGE_IO) {
        return TRUE;
    }
    if (pty) {
        /* PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE takes the HPCON value itself. */
        return g2a_update_attribute(
            attributes, G2A_ATTRIBUTE_PSEUDOCONSOLE, "PSEUDOCONSOLE",
            backing->pseudo_console, sizeof(backing->pseudo_console)
        );
    }
    return g2a_update_attribute(
        attributes, G2A_ATTRIBUTE_HANDLE_LIST, "HANDLE_LIST", backing->io_handles,
        sizeof(backing->io_handles)
    );
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
    G2A_PROCESS_API process_api,
    BOOL no_transport,
    BOOL include_security
) {
    SIZE_T bytes = 0;
    LPPROC_THREAD_ATTRIBUTE_LIST attributes = NULL;
    STARTUPINFOEXW startup;
    G2A_ATTRIBUTE_BACKING backing;
    WCHAR command[32768];
    int count;
    BOOL created = FALSE;
    BOOL attributes_initialized = FALSE;
    LPVOID environment = NULL;
    BOOL descendant_mode = wcsncmp(mode, L"desc-", 5) == 0;
    DWORD attribute_count = (include_security ? 1UL : 0UL) +
        (stage >= G2A_ATTRIBUTE_STAGE_JOB ? 1UL : 0UL) +
        (stage >= G2A_ATTRIBUTE_STAGE_IO ? 1UL : 0UL);
    ZeroMemory(&backing, sizeof(backing));
    backing.security_capabilities.AppContainerSid = profile->Sid;
    backing.security_capabilities.Capabilities = NULL;
    backing.security_capabilities.CapabilityCount = 0;
    backing.security_capabilities.Reserved = 0;
    backing.job_handles[0] = job;
    backing.io_handles[0] = input_read;
    backing.io_handles[1] = output_write;
    backing.io_handles[2] = error_write;
    backing.pseudo_console = pseudo_console;
    if (descendant_mode) {
        count = swprintf_s(command, sizeof(command) / sizeof(command[0]),
            L"\"%s\" child-%s%s \"%s\" \"%s\"", self, mode,
            minimal_child ? L"-minimal" : L"", fixture_root,
            current_directory != NULL ? current_directory : L"");
    } else {
        count = swprintf_s(command, sizeof(command) / sizeof(command[0]),
            L"\"%s\" child-%s%s \"%s\"", self, mode,
            minimal_child ? L"-minimal" : L"", fixture_root);
    }
    if (count <= 0) {
        g2a_emit("G2A_CHILD_CREATE=FAIL\n");
        return FALSE;
    }
    g2a_emit_attribute_contract(include_security, pty, stage);
    g2a_emitf(
        "G2A_PROCESS_API=%s\n",
        process_api == G2A_PROCESS_API_CURRENT ? "CreateProcessW" : "CreateProcessAsUserW"
    );
    g2a_emit_wide("G2A_LP_APPLICATION_NAME=", self);
    g2a_emit_wide("G2A_COMMAND_EXECUTABLE=", self);
    g2a_emit_wide("G2A_COMMAND_LINE=", command);
    g2a_emit_wide("G2A_LP_CURRENT_DIRECTORY=", current_directory);
    g2a_emitf("G2A_INHERIT_HANDLES=%s\n", (!pty && !no_transport) ? "TRUE" : "FALSE");
    g2a_emit("G2A_CREATION_FLAGS=CREATE_UNICODE_ENVIRONMENT,CREATE_NO_WINDOW,EXTENDED_STARTUPINFO_PRESENT\n");
    (void)InitializeProcThreadAttributeList(NULL, attribute_count, 0, &bytes);
    if (bytes == 0) {
        g2a_u32("G2A_ATTRIBUTE_LIST_ERROR=", GetLastError());
        g2a_u32("G2A_CHILD_CREATE_ERROR=", GetLastError());
        return FALSE;
    }
    attributes = (LPPROC_THREAD_ATTRIBUTE_LIST)HeapAlloc(GetProcessHeap(), 0, bytes);
    if (attributes == NULL || !InitializeProcThreadAttributeList(attributes, attribute_count, 0, &bytes)) {
        g2a_u32("G2A_ATTRIBUTE_LIST_ERROR=", GetLastError());
        g2a_u32("G2A_CHILD_CREATE_ERROR=", GetLastError());
        if (attributes != NULL) HeapFree(GetProcessHeap(), 0, attributes);
        return FALSE;
    }
    attributes_initialized = TRUE;
    if (!g2a_validate_job(job, "BEFORE_ATTRIBUTES") ||
        !g2a_update_common_attributes(attributes, &backing, include_security, stage, pty)) {
        g2a_u32("G2A_ATTRIBUTE_LIST_ERROR=", GetLastError());
        g2a_u32("G2A_CHILD_CREATE_ERROR=", GetLastError());
        DeleteProcThreadAttributeList(attributes);
        HeapFree(GetProcessHeap(), 0, attributes);
        return FALSE;
    }
    ZeroMemory(&startup, sizeof(startup));
    startup.StartupInfo.cb = sizeof(startup);
    if (!pty && !no_transport) {
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
                if (attributes_initialized) DeleteProcThreadAttributeList(attributes);
                HeapFree(GetProcessHeap(), 0, attributes);
                return FALSE;
            }
            if (!g2a_validate_job(job, "BEFORE_CREATE")) {
                g2a_emit("G2A_CHILD_CREATE=FAIL\n");
                g2a_u32("G2A_CHILD_CREATE_ERROR=", ERROR_INVALID_HANDLE);
                g2a_destroy_environment(environment);
                CloseHandle(token);
                if (attributes_initialized) DeleteProcThreadAttributeList(attributes);
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
                    !pty && !no_transport,
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
                    !pty && !no_transport,
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
        g2a_attest_external_token(process->hProcess, profile->Sid);
    }
    return created;
}

static void g2a_capture_descendant_pid(const char *text);

static void g2a_parse_variant(
    const wchar_t *mode,
    BOOL *pty,
    BOOL *use_user_environment,
    G2A_ATTRIBUTE_STAGE *stage,
    G2A_PROCESS_API *process_api,
    BOOL *include_security
) {
    *pty = wcsncmp(mode, L"pty", 3) == 0;
    *use_user_environment =
        wcsstr(mode, L"-env-user") != NULL || wcsstr(mode, L"-user") != NULL ||
        wcsstr(mode, L"cd-") != NULL || wcsstr(mode, L"api-") != NULL ||
        wcsstr(mode, L"desc-") != NULL ||
        wcsncmp(mode, L"a0", 2) == 0 || wcsncmp(mode, L"a1", 2) == 0 ||
        wcsncmp(mode, L"a2-", 3) == 0;
    *process_api = wcsstr(mode, L"api-current") != NULL
        ? G2A_PROCESS_API_CURRENT
        : G2A_PROCESS_API_AS_USER;
    *include_security = wcsstr(mode, L"job-only") == NULL;
    if (wcsncmp(mode, L"a0", 2) == 0 || wcsstr(mode, L"-a0") != NULL) {
        *stage = G2A_ATTRIBUTE_STAGE_SECURITY;
    } else if (wcsncmp(mode, L"a1", 2) == 0 || wcsstr(mode, L"-a1") != NULL) {
        *stage = G2A_ATTRIBUTE_STAGE_JOB;
    } else if (wcsncmp(mode, L"a2-", 3) == 0 || wcsstr(mode, L"-a2") != NULL) {
        *stage = G2A_ATTRIBUTE_STAGE_IO;
    } else if (wcsstr(mode, L"cd-") != NULL || wcsstr(mode, L"api-") != NULL) {
        *stage = G2A_ATTRIBUTE_STAGE_SECURITY;
    } else if (wcsncmp(mode, L"desc-", 5) == 0) {
        *stage = G2A_ATTRIBUTE_STAGE_JOB;
    } else {
        *stage = G2A_ATTRIBUTE_STAGE_IO;
    }
}

static G2A_TRANSPORT g2a_transport_for_mode(const wchar_t *mode) {
    if (wcsncmp(mode, L"a0", 2) == 0 || wcsncmp(mode, L"a1", 2) == 0 ||
        wcsstr(mode, L"-a0") != NULL || wcsstr(mode, L"-a1") != NULL) {
        return G2A_TRANSPORT_NONE;
    }
    if (wcsncmp(mode, L"a2-pty", 6) == 0 || wcsncmp(mode, L"pty", 3) == 0) {
        return G2A_TRANSPORT_PTY;
    }
    return G2A_TRANSPORT_PIPE;
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
    if (wcsncmp(mode, L"desc-", 5) == 0) {
        return workspace;
    }
    if (wcsncmp(mode, L"a", 1) == 0) {
        return workspace;
    }
    if (wcsstr(mode, L"-a0") != NULL || wcsstr(mode, L"-a1") != NULL ||
        wcsstr(mode, L"job-only") != NULL) {
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
    G2A_TRANSPORT transport = G2A_TRANSPORT_PIPE;
    BOOL use_user_environment = FALSE;
    BOOL include_security = TRUE;
    G2A_ATTRIBUTE_STAGE stage = G2A_ATTRIBUTE_STAGE_IO;
    G2A_PROCESS_API process_api = G2A_PROCESS_API_AS_USER;
    const wchar_t *current_directory = fixture_root;
    const wchar_t *child_mode = L"pipe";
    BOOL descendant_mode = wcsncmp(mode, L"desc-", 5) == 0;
    BOOL minimal_child = wcsstr(mode, L"min-output") != NULL;
    BOOL input_only = wcsstr(mode, L"input-cr") != NULL || wcsstr(mode, L"input-lf") != NULL;
    BOOL no_descendant = wcsstr(mode, L"full-no-descendant") != NULL;
    ULONGLONG deadline;
    BOOL local_timeout = FALSE;
    DWORD wait_result;
    int result = 0;
    g2a_seen_descendant_pid = 0;
    g2a_parse_variant(mode, &pty, &use_user_environment, &stage, &process_api,
        &include_security);
    transport = g2a_transport_for_mode(mode);
    pty = transport == G2A_TRANSPORT_PTY;
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
    if (stage >= G2A_ATTRIBUTE_STAGE_JOB && !g2a_make_job(&job)) {
        g2a_u32("G2A_JOB_CREATE_ERROR=", GetLastError());
        g2a_profile_cleanup(&profile);
        return 22;
    }
    if (transport == G2A_TRANSPORT_NONE) {
        g2a_emit("G2A_TRANSPORT=NONE\n");
        if (!g2a_create_child(self, L"pipe", fixture_root, current_directory, &profile, job, FALSE,
            NULL, NULL, NULL, NULL, &process, use_user_environment, stage, TRUE,
            process_api, TRUE, include_security)) {
            result = 25;
            goto cleanup;
        }
    } else if (pty) {
        g2a_emit("G2A_TRANSPORT=PTY\n");
        if (no_descendant) {
            child_mode = L"pty-full-no-descendant";
        } else {
            child_mode = (minimal_child || input_only || wcsncmp(mode, L"pty", 3) == 0)
                ? mode : L"pty";
        }
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
        /* CreatePseudoConsole takes ownership of these two ends. */
        if (pty_input_read != NULL) {
            CloseHandle(pty_input_read);
            pty_input_read = NULL;
        }
        if (pty_output_write != NULL) {
            CloseHandle(pty_output_write);
            pty_output_write = NULL;
        }
        g2a_emit("G2A_PTY_CONTROLLER_INPUT_READ_CLOSED=PASS\n");
        g2a_emit("G2A_PTY_CONTROLLER_OUTPUT_WRITE_CLOSED=PASS\n");
        if (!g2a_create_child(self, child_mode, fixture_root, current_directory, &profile, job, TRUE,
            NULL, NULL, NULL, pseudo_console, &process, use_user_environment, stage,
            minimal_child, process_api, FALSE, include_security)) {
            result = 25;
            goto cleanup;
        }
        if (pty_input_write != NULL) {
            const char *input = wcsstr(mode, L"input-cr") != NULL ? "g2a-input\r" : "g2a-input\n";
            DWORD written = 0;
            BOOL write_ok = WriteFile(pty_input_write, input, (DWORD)strlen(input), &written, NULL);
            g2a_emitf("G2A_PTY_STDIN_WRITE=%s|BYTES=%lu\n",
                write_ok ? "PASS" : "FAIL", (unsigned long)written);
        }
    } else {
        g2a_emit("G2A_TRANSPORT=PIPE\n");
        if (descendant_mode) {
            child_mode = mode;
        } else if (no_descendant) {
            child_mode = L"pipe-full-no-descendant";
        }
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
        if (!g2a_create_child(self, child_mode, fixture_root, current_directory, &profile, job, FALSE,
            input_read, output_write, error_write, NULL, &process, use_user_environment, stage,
            minimal_child, process_api, FALSE, include_security)) {
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
        if (job != NULL && IsProcessInJob(process.hProcess, job, &in_job)) {
            g2a_bool("G2A_JOB_MEMBER=", in_job);
        } else if (job != NULL) {
            g2a_u32("G2A_JOB_MEMBER_ERROR=", GetLastError());
        } else {
            g2a_emit("G2A_JOB_MEMBER=NOT_REQUIRED\n");
        }
    }
    deadline = GetTickCount64() + 30000ULL;
    for (;;) {
        if (transport != G2A_TRANSPORT_NONE) {
            g2a_forward_pipe(pty ? pty_output_read : output_read);
        }
        wait_result = WaitForSingleObject(process.hProcess, 20);
        if (wait_result != WAIT_TIMEOUT) {
            break;
        }
        if (GetTickCount64() >= deadline) {
            local_timeout = TRUE;
            g2a_emit("G2A_LOCAL_TIMEOUT=TRUE\n");
            g2a_emit("G2A_LAST_MILESTONE=WAITING_FOR_PROCESS_EXIT\n");
            result = 29;
            break;
        }
    }
    if (transport != G2A_TRANSPORT_NONE) {
        g2a_forward_pipe(pty ? pty_output_read : output_read);
    }
    if (g2a_seen_descendant_pid != 0) {
        DWORD retries = 0;
        while (retries < 50 && !g2a_pid_active(g2a_seen_descendant_pid, &descendant_handle)) {
            Sleep(10);
            retries++;
        }
        g2a_bool("G2A_DESCENDANT_ACTIVE_BEFORE_CLOSE=", descendant_handle != NULL);
        if (descendant_handle != NULL && job != NULL) {
            BOOL descendant_in_job = FALSE;
            if (IsProcessInJob(descendant_handle, job, &descendant_in_job)) {
                g2a_bool("G2A_DESCENDANT_JOB_MEMBER=", descendant_in_job);
            } else {
                g2a_u32("G2A_DESCENDANT_JOB_MEMBER_ERROR=", GetLastError());
            }
        } else if (job == NULL) {
            g2a_emit("G2A_DESCENDANT_JOB_MEMBER=NOT_REQUIRED\n");
        }
    } else {
        g2a_emit("G2A_DESCENDANT_ACTIVE_BEFORE_CLOSE=NOT_OBSERVED\n");
    }
    if (wait_result == WAIT_OBJECT_0) {
        DWORD exit_code = 0;
        (void)GetExitCodeProcess(process.hProcess, &exit_code);
        g2a_u32("G2A_CHILD_EXIT=", exit_code);
    } else if (!local_timeout) {
        g2a_u32("G2A_CHILD_WAIT_ERROR=", GetLastError());
        result = 28;
    }
    if (!local_timeout && wait_result == WAIT_OBJECT_0) {
        g2a_emit("G2A_SCOPE_COMPLETE=PASS\n");
    }
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
    if (argc >= 3 && wcsncmp(argv[1], L"child-desc-", 11) == 0) {
        const wchar_t *workspace = argc >= 4 ? argv[3] : NULL;
        return g2a_child_descendant(argv[2], workspace, argv[1] + 6);
    }
    if (argc >= 3 && wcsncmp(argv[1], L"child-pipe", 10) == 0) {
        BOOL minimal = wcsstr(argv[1], L"minimal") != NULL || wcsstr(argv[1], L"min-output") != NULL;
        BOOL input_only = wcsstr(argv[1], L"input-") != NULL;
        BOOL no_descendant = wcsstr(argv[1], L"no-descendant") != NULL;
        g2a_child_pipe(argv[2], minimal, input_only, no_descendant);
        return 0;
    }
    if (argc >= 3 && wcsncmp(argv[1], L"child-pty", 10) == 0) {
        BOOL minimal = wcsstr(argv[1], L"minimal") != NULL || wcsstr(argv[1], L"min-output") != NULL;
        BOOL input_only = wcsstr(argv[1], L"input-") != NULL;
        BOOL no_descendant = wcsstr(argv[1], L"no-descendant") != NULL;
        g2a_child_pty(argv[2], minimal, input_only, no_descendant);
        return 0;
    }
    if (argc >= 4 && (
        wcsncmp(argv[1], L"pipe", 4) == 0 ||
        wcsncmp(argv[1], L"pty", 3) == 0 ||
        wcsncmp(argv[1], L"cd-", 3) == 0 ||
        wcsncmp(argv[1], L"api-", 4) == 0 ||
        wcsncmp(argv[1], L"layer-", 6) == 0 ||
        wcsncmp(argv[1], L"a0", 2) == 0 ||
        wcsncmp(argv[1], L"a1", 2) == 0 ||
        wcsncmp(argv[1], L"a2-", 3) == 0 ||
        wcsncmp(argv[1], L"desc-", 5) == 0
    )) {
        return g2a_controller(argv[1], argv[2], argv[3]);
    }
    g2a_emit("G2A_INVALID_ARGUMENTS\n");
    return 30;
}
