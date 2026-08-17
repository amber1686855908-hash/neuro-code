#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

/*
 * W5 Gate 1.19 is evidence-only.  This probe deliberately uses only
 * read/query APIs.  It never changes an ACL, token, registry key, device
 * state, or system policy, and it never sends an IOCTL to the target.
 */
#include <windows.h>
#include <sddl.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <wchar.h>

#pragma warning(disable: 4191)

#define GATE119_MAX_BUFFER (64UL * 1024UL)
#define GATE119_MAX_ACE_COUNT 256UL
#define GATE119_MAX_GROUP_COUNT 512UL
#define GATE119_MAX_PRIVILEGE_COUNT 256UL
#define GATE119_STATUS_SUCCESS 0x00000000UL
#define GATE119_FILE_READ_DATA 0x00000001UL
#define GATE119_FILE_WRITE_DATA 0x00000002UL
#define GATE119_SYNCHRONIZE 0x00100000UL
#define GATE119_READ_CONTROL 0x00020000UL
#define GATE119_SECURITY_OWNER 0x00000001UL
#define GATE119_SECURITY_GROUP 0x00000002UL
#define GATE119_SECURITY_DACL 0x00000004UL
#define GATE119_TOKEN_USER ((TOKEN_INFORMATION_CLASS)1)
#define GATE119_TOKEN_GROUPS ((TOKEN_INFORMATION_CLASS)2)
#define GATE119_TOKEN_PRIVILEGES ((TOKEN_INFORMATION_CLASS)3)
#define GATE119_TOKEN_OWNER ((TOKEN_INFORMATION_CLASS)4)
#define GATE119_TOKEN_PRIMARY_GROUP ((TOKEN_INFORMATION_CLASS)5)
#define GATE119_TOKEN_RESTRICTED_SIDS ((TOKEN_INFORMATION_CLASS)11)
#define GATE119_TOKEN_INTEGRITY_LEVEL ((TOKEN_INFORMATION_CLASS)25)
#define GATE119_TOKEN_ELEVATION_TYPE ((TOKEN_INFORMATION_CLASS)18)
#define GATE119_TOKEN_TYPE ((TOKEN_INFORMATION_CLASS)8)
#define GATE119_TOKEN_IMPERSONATION_LEVEL ((TOKEN_INFORMATION_CLASS)9)
#define GATE119_TOKEN_MANDATORY_POLICY ((TOKEN_INFORMATION_CLASS)27)
#define GATE119_TOKEN_IS_APP_CONTAINER ((TOKEN_INFORMATION_CLASS)29)
#define GATE119_TOKEN_APP_CONTAINER_SID ((TOKEN_INFORMATION_CLASS)31)
#define GATE119_TOKEN_CAPABILITIES ((TOKEN_INFORMATION_CLASS)30)
#define GATE119_TOKEN_UI_ACCESS ((TOKEN_INFORMATION_CLASS)26)
#define GATE119_TOKEN_VIRTUALIZATION_ALLOWED ((TOKEN_INFORMATION_CLASS)23)
#define GATE119_TOKEN_VIRTUALIZATION_ENABLED ((TOKEN_INFORMATION_CLASS)24)

typedef LONG GATE119_NTSTATUS;

typedef struct _GATE119_UNICODE_STRING {
    USHORT Length;
    USHORT MaximumLength;
    PWSTR Buffer;
} GATE119_UNICODE_STRING;

typedef struct _GATE119_OBJECT_ATTRIBUTES {
    ULONG Length;
    HANDLE RootDirectory;
    GATE119_UNICODE_STRING *ObjectName;
    ULONG Attributes;
    PVOID SecurityDescriptor;
    PVOID SecurityQualityOfService;
} GATE119_OBJECT_ATTRIBUTES;

typedef struct _GATE119_IO_STATUS_BLOCK {
    GATE119_NTSTATUS Status;
    ULONG Reserved;
    ULONG_PTR Information;
} GATE119_IO_STATUS_BLOCK;

typedef GATE119_NTSTATUS (NTAPI *gate119_nt_open_file_fn)(
    PHANDLE FileHandle,
    ACCESS_MASK DesiredAccess,
    GATE119_OBJECT_ATTRIBUTES *ObjectAttributes,
    GATE119_IO_STATUS_BLOCK *IoStatusBlock,
    ULONG ShareAccess,
    ULONG OpenOptions
);

typedef GATE119_NTSTATUS (NTAPI *gate119_nt_query_security_object_fn)(
    HANDLE Handle,
    SECURITY_INFORMATION SecurityInformation,
    PSECURITY_DESCRIPTOR SecurityDescriptor,
    ULONG Length,
    PULONG LengthNeeded
);

static void emit_ascii(const char *text) {
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output != NULL && output != INVALID_HANDLE_VALUE) {
        (void)WriteFile(output, text, (DWORD)lstrlenA(text), &written, NULL);
    }
}

static void emit_format(const char *format, ...) {
    char line[1024];
    va_list arguments;
    va_start(arguments, format);
    (void)vsnprintf_s(line, sizeof(line), _TRUNCATE, format, arguments);
    va_end(arguments);
    emit_ascii(line);
}

static void emit_u32(const char *prefix, ULONG value) {
    emit_format("%s%lu\n", prefix, (unsigned long)value);
}

static void emit_hex32(const char *prefix, ULONG value) {
    emit_format("%s0x%08lX\n", prefix, (unsigned long)value);
}

static void emit_hex64(const char *prefix, ULONG_PTR value) {
    emit_format("%s0x%llX\n", prefix, (unsigned long long)value);
}

static void emit_bool(const char *prefix, BOOL value) {
    emit_format("%s%s\n", prefix, value ? "PASS" : "FAIL");
}

static BOOL sid_to_utf8(PSID sid, char *output, size_t capacity) {
    LPWSTR sid_text = NULL;
    int converted;
    if (sid == NULL || !IsValidSid(sid) || output == NULL || capacity == 0 ||
        !ConvertSidToStringSidW(sid, &sid_text) || sid_text == NULL) {
        return FALSE;
    }
    converted = WideCharToMultiByte(
        CP_UTF8,
        0,
        sid_text,
        -1,
        output,
        (int)capacity,
        NULL,
        NULL
    );
    LocalFree(sid_text);
    return converted > 0;
}

static void emit_sid_field(const char *kind, const char *field, PSID sid) {
    char text[128];
    if (sid_to_utf8(sid, text, sizeof(text))) {
        emit_format("W5_GATE119_TOKEN_SID=%s|FIELD=%s|SID=%s\n", kind, field, text);
    } else {
        emit_format("W5_GATE119_TOKEN_SID=%s|FIELD=%s|SID=UNAVAILABLE\n", kind, field);
    }
}

static void emit_field_status(const char *field, BOOL supported, DWORD error) {
    emit_format(
        "W5_GATE119_TOKEN_FIELD=%s|SUPPORTED=%d|ERROR=%lu\n",
        field,
        supported ? 1 : 0,
        (unsigned long)error
    );
}

static BOOL query_token_info(
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
    if (*error != ERROR_INSUFFICIENT_BUFFER || required == 0 || required > GATE119_MAX_BUFFER) {
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

static void emit_sid_query(
    HANDLE token,
    const char *field,
    TOKEN_INFORMATION_CLASS information_class,
    BOOL owner_kind
) {
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    PSID sid = NULL;
    if (!query_token_info(token, information_class, &buffer, &size, &error)) {
        emit_field_status(field, FALSE, error);
        return;
    }
    (void)size;
    if (owner_kind == 1) {
        sid = ((TOKEN_OWNER *)buffer)->Owner;
    } else if (owner_kind == 2) {
        sid = ((TOKEN_PRIMARY_GROUP *)buffer)->PrimaryGroup;
    } else {
        sid = ((TOKEN_USER *)buffer)->User.Sid;
    }
    emit_field_status(field, TRUE, ERROR_SUCCESS);
    emit_sid_field("SID", field, sid);
    HeapFree(GetProcessHeap(), 0, buffer);
}

static void emit_group_query(
    HANDLE token,
    const char *field,
    TOKEN_INFORMATION_CLASS information_class
) {
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    TOKEN_GROUPS *groups;
    DWORD index;
    if (!query_token_info(token, information_class, &buffer, &size, &error)) {
        emit_field_status(field, FALSE, error);
        return;
    }
    (void)size;
    groups = (TOKEN_GROUPS *)buffer;
    emit_field_status(field, TRUE, ERROR_SUCCESS);
    emit_u32("W5_GATE119_TOKEN_GROUP_COUNT=", groups->GroupCount);
    if (groups->GroupCount > GATE119_MAX_GROUP_COUNT) {
        emit_ascii("W5_GATE119_TOKEN_GROUP_TRUNCATED=1\n");
    }
    for (index = 0; index < groups->GroupCount && index < GATE119_MAX_GROUP_COUNT; ++index) {
        char sid_text[128];
        if (sid_to_utf8(groups->Groups[index].Sid, sid_text, sizeof(sid_text))) {
            emit_format(
                "W5_GATE119_TOKEN_GROUP=%s|SID=%s|ATTR=0x%08lX\n",
                field,
                sid_text,
                (unsigned long)groups->Groups[index].Attributes
            );
        }
    }
    HeapFree(GetProcessHeap(), 0, buffer);
}

static void emit_privilege_query(HANDLE token) {
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    TOKEN_PRIVILEGES *privileges;
    DWORD index;
    if (!query_token_info(token, GATE119_TOKEN_PRIVILEGES, &buffer, &size, &error)) {
        emit_field_status("TokenPrivileges", FALSE, error);
        return;
    }
    (void)size;
    privileges = (TOKEN_PRIVILEGES *)buffer;
    emit_field_status("TokenPrivileges", TRUE, ERROR_SUCCESS);
    emit_u32("W5_GATE119_TOKEN_PRIVILEGE_COUNT=", privileges->PrivilegeCount);
    if (privileges->PrivilegeCount > GATE119_MAX_PRIVILEGE_COUNT) {
        emit_ascii("W5_GATE119_TOKEN_PRIVILEGE_TRUNCATED=1\n");
    }
    for (index = 0; index < privileges->PrivilegeCount && index < GATE119_MAX_PRIVILEGE_COUNT; ++index) {
        wchar_t name[256];
        DWORD name_length = (DWORD)(sizeof(name) / sizeof(name[0]));
        char utf8[256];
        LUID luid = privileges->Privileges[index].Luid;
        BOOL has_name = LookupPrivilegeNameW(NULL, &luid, name, &name_length);
        if (has_name && WideCharToMultiByte(CP_UTF8, 0, name, -1, utf8, (int)sizeof(utf8), NULL, NULL) > 0) {
            emit_format(
                "W5_GATE119_TOKEN_PRIVILEGE=NAME=%s|ATTR=0x%08lX|LUID=0x%08lX%08lX\n",
                utf8,
                (unsigned long)privileges->Privileges[index].Attributes,
                (unsigned long)luid.HighPart,
                (unsigned long)luid.LowPart
            );
        } else {
            emit_format(
                "W5_GATE119_TOKEN_PRIVILEGE=NAME=UNAVAILABLE|ATTR=0x%08lX|LUID=0x%08lX%08lX\n",
                (unsigned long)privileges->Privileges[index].Attributes,
                (unsigned long)luid.HighPart,
                (unsigned long)luid.LowPart
            );
        }
    }
    HeapFree(GetProcessHeap(), 0, buffer);
}

static void emit_scalar_query(
    HANDLE token,
    const char *field,
    TOKEN_INFORMATION_CLASS information_class,
    DWORD size_expected
) {
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    if (!query_token_info(token, information_class, &buffer, &size, &error)) {
        emit_field_status(field, FALSE, error);
        return;
    }
    (void)size;
    emit_field_status(field, TRUE, ERROR_SUCCESS);
    if (size < size_expected) {
        emit_format("W5_GATE119_TOKEN_SCALAR=%s|VALUE=UNAVAILABLE\n", field);
    } else if (information_class == GATE119_TOKEN_IS_APP_CONTAINER ||
        information_class == GATE119_TOKEN_UI_ACCESS ||
        information_class == GATE119_TOKEN_VIRTUALIZATION_ALLOWED ||
        information_class == GATE119_TOKEN_VIRTUALIZATION_ENABLED) {
        emit_format("W5_GATE119_TOKEN_SCALAR=%s|VALUE=%d\n", field, *(BOOL *)buffer ? 1 : 0);
    } else if (information_class == GATE119_TOKEN_ELEVATION_TYPE) {
        emit_format("W5_GATE119_TOKEN_SCALAR=%s|VALUE=%lu\n", field, (unsigned long)*(TOKEN_ELEVATION_TYPE *)buffer);
    } else if (information_class == GATE119_TOKEN_TYPE) {
        emit_format("W5_GATE119_TOKEN_SCALAR=%s|VALUE=%lu\n", field, (unsigned long)*(TOKEN_TYPE *)buffer);
    } else if (information_class == GATE119_TOKEN_IMPERSONATION_LEVEL) {
        emit_format("W5_GATE119_TOKEN_SCALAR=%s|VALUE=%lu\n", field, (unsigned long)*(SECURITY_IMPERSONATION_LEVEL *)buffer);
    } else if (information_class == GATE119_TOKEN_MANDATORY_POLICY) {
        emit_format("W5_GATE119_TOKEN_SCALAR=%s|VALUE=0x%08lX\n", field, (unsigned long)((TOKEN_MANDATORY_POLICY *)buffer)->Policy);
    } else {
        emit_format("W5_GATE119_TOKEN_SCALAR=%s|VALUE=UNSUPPORTED_SIZE\n", field);
    }
    HeapFree(GetProcessHeap(), 0, buffer);
}

static void emit_integrity_query(HANDLE token) {
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    TOKEN_MANDATORY_LABEL *label;
    DWORD count;
    DWORD rid;
    if (!query_token_info(token, GATE119_TOKEN_INTEGRITY_LEVEL, &buffer, &size, &error)) {
        emit_field_status("TokenIntegrityLevel", FALSE, error);
        return;
    }
    (void)size;
    emit_field_status("TokenIntegrityLevel", TRUE, ERROR_SUCCESS);
    label = (TOKEN_MANDATORY_LABEL *)buffer;
    count = GetSidSubAuthorityCount(label->Label.Sid) == NULL
        ? 0
        : *GetSidSubAuthorityCount(label->Label.Sid);
    rid = count == 0 ? 0 : *GetSidSubAuthority(label->Label.Sid, count - 1);
    emit_u32("W5_GATE119_TOKEN_INTEGRITY_RID=", rid);
    emit_u32("W5_GATE119_TOKEN_INTEGRITY_ATTR=", label->Label.Attributes);
    HeapFree(GetProcessHeap(), 0, buffer);
}

static void emit_app_container_sid_query(HANDLE token) {
    BYTE *buffer = NULL;
    DWORD size = 0;
    DWORD error = ERROR_SUCCESS;
    TOKEN_APPCONTAINER_INFORMATION *information;
    if (!query_token_info(token, GATE119_TOKEN_APP_CONTAINER_SID, &buffer, &size, &error)) {
        emit_field_status("TokenAppContainerSid", FALSE, error);
        return;
    }
    (void)size;
    emit_field_status("TokenAppContainerSid", TRUE, ERROR_SUCCESS);
    information = (TOKEN_APPCONTAINER_INFORMATION *)buffer;
    emit_sid_field("SID", "TokenAppContainerSid", information->TokenAppContainer);
    HeapFree(GetProcessHeap(), 0, buffer);
}

static void emit_token_fingerprint(void) {
    HANDLE token = NULL;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        emit_u32("W5_GATE119_TOKEN_OPEN_ERROR=", GetLastError());
        return;
    }
    emit_sid_query(token, "TokenUser", GATE119_TOKEN_USER, 0);
    emit_sid_query(token, "TokenOwner", GATE119_TOKEN_OWNER, 1);
    emit_sid_query(token, "TokenPrimaryGroup", GATE119_TOKEN_PRIMARY_GROUP, 2);
    emit_group_query(token, "TokenGroups", GATE119_TOKEN_GROUPS);
    emit_group_query(token, "TokenRestrictedSids", GATE119_TOKEN_RESTRICTED_SIDS);
    emit_privilege_query(token);
    emit_integrity_query(token);
    emit_scalar_query(token, "TokenElevationType", GATE119_TOKEN_ELEVATION_TYPE, sizeof(TOKEN_ELEVATION_TYPE));
    emit_scalar_query(token, "TokenType", GATE119_TOKEN_TYPE, sizeof(TOKEN_TYPE));
    emit_scalar_query(token, "TokenImpersonationLevel", GATE119_TOKEN_IMPERSONATION_LEVEL, sizeof(SECURITY_IMPERSONATION_LEVEL));
    emit_scalar_query(token, "TokenMandatoryPolicy", GATE119_TOKEN_MANDATORY_POLICY, sizeof(TOKEN_MANDATORY_POLICY));
    emit_scalar_query(token, "TokenIsAppContainer", GATE119_TOKEN_IS_APP_CONTAINER, sizeof(BOOL));
    emit_app_container_sid_query(token);
    emit_group_query(token, "TokenCapabilities", GATE119_TOKEN_CAPABILITIES);
    emit_scalar_query(token, "TokenUIAccess", GATE119_TOKEN_UI_ACCESS, sizeof(BOOL));
    emit_scalar_query(token, "TokenVirtualizationAllowed", GATE119_TOKEN_VIRTUALIZATION_ALLOWED, sizeof(BOOL));
    emit_scalar_query(token, "TokenVirtualizationEnabled", GATE119_TOKEN_VIRTUALIZATION_ENABLED, sizeof(BOOL));
    emit_bool("W5_GATE119_TOKEN_QUERY_CLOSED=", CloseHandle(token));
}

static unsigned long long parse_u64(const wchar_t *text) {
    return _wcstoui64(text, NULL, 0);
}

static BOOL build_object_attributes(
    const wchar_t *object_text,
    ULONG attributes_length,
    ULONG object_attributes_value,
    GATE119_UNICODE_STRING *object_name,
    GATE119_OBJECT_ATTRIBUTES *object_attributes
) {
    size_t characters = wcslen(object_text);
    if (characters > 0x7FFF || characters * sizeof(wchar_t) > 0xFFFE) {
        return FALSE;
    }
    object_name->Length = (USHORT)(characters * sizeof(wchar_t));
    object_name->MaximumLength = (USHORT)((characters + 1) * sizeof(wchar_t));
    object_name->Buffer = (PWSTR)object_text;
    ZeroMemory(object_attributes, sizeof(*object_attributes));
    object_attributes->Length = attributes_length;
    object_attributes->ObjectName = object_name;
    object_attributes->Attributes = object_attributes_value;
    return TRUE;
}

static BOOL open_target(
    const wchar_t *object_text,
    ACCESS_MASK desired_access,
    ULONG object_attributes_value,
    ULONG share_access,
    ULONG open_options,
    HANDLE *handle,
    GATE119_NTSTATUS *status,
    GATE119_IO_STATUS_BLOCK *io_status
) {
    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    gate119_nt_open_file_fn nt_open_file;
    GATE119_UNICODE_STRING object_name;
    GATE119_OBJECT_ATTRIBUTES object_attributes;
    if (ntdll == NULL || handle == NULL || status == NULL || io_status == NULL ||
        !build_object_attributes(object_text, (ULONG)sizeof(GATE119_OBJECT_ATTRIBUTES), 0, &object_name, &object_attributes)) {
        return FALSE;
    }
    nt_open_file = (gate119_nt_open_file_fn)GetProcAddress(ntdll, "NtOpenFile");
    if (nt_open_file == NULL) {
        return FALSE;
    }
    ZeroMemory(io_status, sizeof(*io_status));
    *handle = NULL;
    *status = nt_open_file(
        handle,
        desired_access,
        &object_attributes,
        io_status,
        share_access,
        open_options
    );
    return TRUE;
}

static void wait_for_release(void) {
    char byte;
    DWORD read = 0;
    HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    if (input != NULL && input != INVALID_HANDLE_VALUE) {
        (void)ReadFile(input, &byte, 1, &read, NULL);
    }
}

static int run_ntopen(const wchar_t *object_text, ACCESS_MASK desired_access) {
    HANDLE handle = NULL;
    GATE119_NTSTATUS status = 0;
    GATE119_IO_STATUS_BLOCK io_status;
    emit_ascii("W5_GATE119_NTOPEN_STARTED=OBSERVED\n");
    if (!open_target(object_text, desired_access, 0, 0x7, 0x20, &handle, &status, &io_status)) {
        emit_ascii("W5_GATE119_NTOPEN=UNAVAILABLE\n");
        return 32;
    }
    emit_hex32("W5_GATE119_NTOPEN_STATUS=", (ULONG)status);
    emit_hex32("W5_GATE119_NTOPEN_IO_STATUS=", (ULONG)io_status.Status);
    emit_hex64("W5_GATE119_NTOPEN_IO_INFORMATION=", io_status.Information);
    if ((ULONG)status == GATE119_STATUS_SUCCESS && handle != NULL && handle != INVALID_HANDLE_VALUE) {
        emit_bool("W5_GATE119_NTOPEN_HANDLE_CLOSE=", CloseHandle(handle));
    }
    emit_ascii("W5_GATE119_NTOPEN_FINISHED=OBSERVED\n");
    return (ULONG)status == GATE119_STATUS_SUCCESS ? 0 : 24;
}

static void emit_descriptor_sid(const char *field, PSID sid) {
    char sid_text[128];
    if (sid_to_utf8(sid, sid_text, sizeof(sid_text))) {
        emit_format("W5_GATE119_DESCRIPTOR_%s=%s\n", field, sid_text);
    } else {
        emit_format("W5_GATE119_DESCRIPTOR_%s=UNAVAILABLE\n", field);
    }
}

static void emit_descriptor_aces(PACL dacl) {
    ACL_SIZE_INFORMATION information;
    DWORD index;
    if (dacl == NULL || !GetAclInformation(dacl, &information, sizeof(information), AclSizeInformation)) {
        emit_u32("W5_GATE119_DESCRIPTOR_DACL_QUERY_ERROR=", GetLastError());
        return;
    }
    emit_u32("W5_GATE119_DESCRIPTOR_ACE_COUNT=", information.AceCount);
    if (information.AceCount > GATE119_MAX_ACE_COUNT) {
        emit_ascii("W5_GATE119_DESCRIPTOR_ACE_TRUNCATED=1\n");
    }
    for (index = 0; index < information.AceCount && index < GATE119_MAX_ACE_COUNT; ++index) {
        LPVOID ace_pointer = NULL;
        ACE_HEADER *header;
        DWORD mask = 0;
        PSID sid = NULL;
        if (!GetAce(dacl, index, &ace_pointer) || ace_pointer == NULL) {
            emit_format("W5_GATE119_DESCRIPTOR_ACE=INDEX=%lu|ERROR=%lu\n", (unsigned long)index, (unsigned long)GetLastError());
            continue;
        }
        header = (ACE_HEADER *)ace_pointer;
        if (header->AceType == ACCESS_ALLOWED_ACE_TYPE) {
            ACCESS_ALLOWED_ACE *ace = (ACCESS_ALLOWED_ACE *)ace_pointer;
            mask = ace->Mask;
            sid = (PSID)&ace->SidStart;
        } else if (header->AceType == ACCESS_DENIED_ACE_TYPE) {
            ACCESS_DENIED_ACE *ace = (ACCESS_DENIED_ACE *)ace_pointer;
            mask = ace->Mask;
            sid = (PSID)&ace->SidStart;
        }
        {
            char sid_text[128];
            if (sid != NULL && sid_to_utf8(sid, sid_text, sizeof(sid_text))) {
                emit_format(
                    "W5_GATE119_DESCRIPTOR_ACE=INDEX=%lu|TYPE=%u|FLAGS=0x%02X|MASK=0x%08lX|SID=%s\n",
                    (unsigned long)index,
                    (unsigned int)header->AceType,
                    (unsigned int)header->AceFlags,
                    (unsigned long)mask,
                    sid_text
                );
            } else {
                emit_format(
                    "W5_GATE119_DESCRIPTOR_ACE=INDEX=%lu|TYPE=%u|FLAGS=0x%02X|MASK=0x%08lX|SID=UNAVAILABLE\n",
                    (unsigned long)index,
                    (unsigned int)header->AceType,
                    (unsigned int)header->AceFlags,
                    (unsigned long)mask
                );
            }
        }
    }
}

static int run_security_descriptor(const wchar_t *object_text) {
    HANDLE handle = NULL;
    GATE119_NTSTATUS status = 0;
    GATE119_IO_STATUS_BLOCK io_status;
    HMODULE ntdll;
    gate119_nt_query_security_object_fn query_security;
    PSECURITY_DESCRIPTOR descriptor = NULL;
    DWORD required = 0;
    DWORD error = ERROR_SUCCESS;
    BOOL present = FALSE;
    BOOL defaulted = FALSE;
    PSID owner = NULL;
    PSID group = NULL;
    PACL dacl = NULL;
    emit_ascii("W5_GATE119_SECURITY_STARTED=OBSERVED\n");
    if (!open_target(object_text, GATE119_READ_CONTROL, 0, 0x7, 0x20, &handle, &status, &io_status)) {
        emit_ascii("W5_GATE119_SECURITY_OPEN=UNAVAILABLE\n");
        return 32;
    }
    emit_hex32("W5_GATE119_SECURITY_OPEN_STATUS=", (ULONG)status);
    if ((ULONG)status != GATE119_STATUS_SUCCESS || handle == NULL || handle == INVALID_HANDLE_VALUE) {
        emit_ascii("W5_GATE119_SECURITY_QUERY=NOT_ATTEMPTED\n");
        emit_ascii("W5_GATE119_SECURITY_FINISHED=OBSERVED\n");
        return 24;
    }
    ntdll = GetModuleHandleW(L"ntdll.dll");
    query_security = ntdll == NULL
        ? NULL
        : (gate119_nt_query_security_object_fn)GetProcAddress(ntdll, "NtQuerySecurityObject");
    if (query_security == NULL) {
        emit_ascii("W5_GATE119_SECURITY_QUERY=UNAVAILABLE\n");
        emit_bool("W5_GATE119_SECURITY_HANDLE_CLOSE=", CloseHandle(handle));
        emit_ascii("W5_GATE119_SECURITY_FINISHED=OBSERVED\n");
        return 25;
    }
    status = query_security(
        handle,
        GATE119_SECURITY_OWNER | GATE119_SECURITY_GROUP | GATE119_SECURITY_DACL,
        NULL,
        0,
        &required
    );
    emit_hex32("W5_GATE119_SECURITY_QUERY_SIZE_STATUS=", (ULONG)status);
    emit_u32("W5_GATE119_SECURITY_QUERY_SIZE=", required);
    if (required == 0 || required > GATE119_MAX_BUFFER) {
        emit_hex32("W5_GATE119_SECURITY_QUERY_STATUS=", (ULONG)status);
        emit_bool("W5_GATE119_SECURITY_HANDLE_CLOSE=", CloseHandle(handle));
        emit_ascii("W5_GATE119_SECURITY_FINISHED=OBSERVED\n");
        return 26;
    }
    descriptor = (PSECURITY_DESCRIPTOR)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, required);
    if (descriptor == NULL) {
        emit_ascii("W5_GATE119_SECURITY_QUERY=OUT_OF_MEMORY\n");
        emit_bool("W5_GATE119_SECURITY_HANDLE_CLOSE=", CloseHandle(handle));
        emit_ascii("W5_GATE119_SECURITY_FINISHED=OBSERVED\n");
        return 27;
    }
    status = query_security(
        handle,
        GATE119_SECURITY_OWNER | GATE119_SECURITY_GROUP | GATE119_SECURITY_DACL,
        descriptor,
        required,
        &required
    );
    emit_hex32("W5_GATE119_SECURITY_QUERY_STATUS=", (ULONG)status);
    if ((ULONG)status == GATE119_STATUS_SUCCESS) {
        if (!GetSecurityDescriptorOwner(descriptor, &owner, &defaulted)) {
            error = GetLastError();
            emit_u32("W5_GATE119_SECURITY_OWNER_ERROR=", error);
        } else {
            emit_descriptor_sid("OWNER", owner);
        }
        if (!GetSecurityDescriptorGroup(descriptor, &group, &defaulted)) {
            error = GetLastError();
            emit_u32("W5_GATE119_SECURITY_GROUP_ERROR=", error);
        } else {
            emit_descriptor_sid("GROUP", group);
        }
        if (!GetSecurityDescriptorDacl(descriptor, &present, &dacl, &defaulted)) {
            error = GetLastError();
            emit_u32("W5_GATE119_SECURITY_DACL_ERROR=", error);
        } else {
            emit_format("W5_GATE119_SECURITY_DACL_PRESENT=%d\n", present ? 1 : 0);
            emit_format("W5_GATE119_SECURITY_DACL_NULL=%d\n", present && dacl == NULL ? 1 : 0);
            if (present && dacl != NULL) {
                emit_descriptor_aces(dacl);
            }
        }
    }
    HeapFree(GetProcessHeap(), 0, descriptor);
    emit_bool("W5_GATE119_SECURITY_HANDLE_CLOSE=", CloseHandle(handle));
    emit_ascii("W5_GATE119_SECURITY_FINISHED=OBSERVED\n");
    return (ULONG)status == GATE119_STATUS_SUCCESS ? 0 : 24;
}

int wmain(int argc, wchar_t **argv) {
    DWORD process_id = GetCurrentProcessId();
    if (argc < 2) {
        emit_ascii("W5_GATE119_ARGUMENTS=FAIL\n");
        return 30;
    }
    emit_u32("W5_GATE116_PID=", process_id);
    emit_ascii("W5_GATE116_READY=OBSERVED\n");
    emit_ascii("W5_GATE119_PROBE_STARTED=OBSERVED\n");
    if (wcscmp(argv[1], L"fingerprint") == 0) {
        emit_token_fingerprint();
    } else if (wcscmp(argv[1], L"ntopen") == 0 && argc >= 4) {
        (void)run_ntopen(argv[2], (ACCESS_MASK)parse_u64(argv[3]));
    } else if (wcscmp(argv[1], L"security") == 0 && argc >= 3) {
        (void)run_security_descriptor(argv[2]);
    } else {
        emit_ascii("W5_GATE119_ARGUMENTS=FAIL\n");
    }
    wait_for_release();
    emit_ascii("W5_GATE119_PROBE_FINISHED=OBSERVED\n");
    return 0;
}
