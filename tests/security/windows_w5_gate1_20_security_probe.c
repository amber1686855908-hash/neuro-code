#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

/* W5 Gate 1.20 read-only NtOpenFile/NtQuerySecurityObject probe. */
#include <windows.h>
#include <sddl.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <wchar.h>

#pragma warning(disable: 4191)

#define GATE120_MAX_BUFFER (64UL * 1024UL)
#define GATE120_MAX_ACE_COUNT 256UL
#define GATE120_STATUS_SUCCESS 0x00000000UL
#define GATE120_FILE_READ_DATA 0x00000001UL
#define GATE120_FILE_WRITE_DATA 0x00000002UL
#define GATE120_SYNCHRONIZE 0x00100000UL
#define GATE120_READ_CONTROL 0x00020000UL
#define GATE120_SECURITY_OWNER 0x00000001UL
#define GATE120_SECURITY_GROUP 0x00000002UL
#define GATE120_SECURITY_DACL 0x00000004UL

typedef LONG GATE120_NTSTATUS;

typedef struct _GATE120_UNICODE_STRING {
    USHORT Length;
    USHORT MaximumLength;
    PWSTR Buffer;
} GATE120_UNICODE_STRING;

typedef struct _GATE120_OBJECT_ATTRIBUTES {
    ULONG Length;
    HANDLE RootDirectory;
    GATE120_UNICODE_STRING *ObjectName;
    ULONG Attributes;
    PVOID SecurityDescriptor;
    PVOID SecurityQualityOfService;
} GATE120_OBJECT_ATTRIBUTES;

typedef struct _GATE120_IO_STATUS_BLOCK {
    GATE120_NTSTATUS Status;
    ULONG Reserved;
    ULONG_PTR Information;
} GATE120_IO_STATUS_BLOCK;

typedef GATE120_NTSTATUS (NTAPI *gate120_nt_open_file_fn)(
    PHANDLE FileHandle,
    ACCESS_MASK DesiredAccess,
    GATE120_OBJECT_ATTRIBUTES *ObjectAttributes,
    GATE120_IO_STATUS_BLOCK *IoStatusBlock,
    ULONG ShareAccess,
    ULONG OpenOptions
);

typedef GATE120_NTSTATUS (NTAPI *gate120_nt_query_security_object_fn)(
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

static BOOL build_object_attributes(
    const wchar_t *object_text,
    GATE120_UNICODE_STRING *object_name,
    GATE120_OBJECT_ATTRIBUTES *object_attributes
) {
    size_t characters = wcslen(object_text);
    if (characters > 0x7FFF || characters * sizeof(wchar_t) > 0xFFFE) {
        return FALSE;
    }
    object_name->Length = (USHORT)(characters * sizeof(wchar_t));
    object_name->MaximumLength = (USHORT)((characters + 1) * sizeof(wchar_t));
    object_name->Buffer = (PWSTR)object_text;
    ZeroMemory(object_attributes, sizeof(*object_attributes));
    object_attributes->Length = (ULONG)sizeof(GATE120_OBJECT_ATTRIBUTES);
    object_attributes->ObjectName = object_name;
    object_attributes->Attributes = 0;
    return TRUE;
}

static BOOL open_target(
    const wchar_t *object_text,
    ACCESS_MASK desired_access,
    HANDLE *handle,
    GATE120_NTSTATUS *status,
    GATE120_IO_STATUS_BLOCK *io_status
) {
    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    gate120_nt_open_file_fn nt_open_file;
    GATE120_UNICODE_STRING object_name;
    GATE120_OBJECT_ATTRIBUTES object_attributes;
    if (ntdll == NULL || handle == NULL || status == NULL || io_status == NULL ||
        !build_object_attributes(object_text, &object_name, &object_attributes)) {
        return FALSE;
    }
    nt_open_file = (gate120_nt_open_file_fn)GetProcAddress(ntdll, "NtOpenFile");
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
        0x7,
        0x20
    );
    return TRUE;
}

static void emit_descriptor_sid(const char *field, PSID sid) {
    char sid_text[128];
    if (sid_to_utf8(sid, sid_text, sizeof(sid_text))) {
        emit_format("W5_GATE120_DESCRIPTOR_%s=%s\n", field, sid_text);
    } else {
        emit_format("W5_GATE120_DESCRIPTOR_%s=UNAVAILABLE\n", field);
    }
}

static void emit_descriptor_aces(PACL dacl) {
    ACL_SIZE_INFORMATION information;
    DWORD index;
    if (dacl == NULL || !GetAclInformation(
        dacl,
        &information,
        sizeof(information),
        AclSizeInformation
    )) {
        emit_u32("W5_GATE120_DESCRIPTOR_DACL_QUERY_ERROR=", GetLastError());
        return;
    }
    emit_u32("W5_GATE120_DESCRIPTOR_ACE_COUNT=", information.AceCount);
    for (index = 0; index < information.AceCount && index < GATE120_MAX_ACE_COUNT; ++index) {
        LPVOID pointer = NULL;
        ACE_HEADER *header;
        DWORD mask = 0;
        PSID sid = NULL;
        char sid_text[128];
        if (!GetAce(dacl, index, &pointer) || pointer == NULL) {
            emit_format(
                "W5_GATE120_DESCRIPTOR_ACE=INDEX=%lu|ERROR=%lu\n",
                (unsigned long)index,
                (unsigned long)GetLastError()
            );
            continue;
        }
        header = (ACE_HEADER *)pointer;
        if (header->AceType == ACCESS_ALLOWED_ACE_TYPE) {
            ACCESS_ALLOWED_ACE *ace = (ACCESS_ALLOWED_ACE *)pointer;
            mask = ace->Mask;
            sid = (PSID)&ace->SidStart;
        } else if (header->AceType == ACCESS_DENIED_ACE_TYPE) {
            ACCESS_DENIED_ACE *ace = (ACCESS_DENIED_ACE *)pointer;
            mask = ace->Mask;
            sid = (PSID)&ace->SidStart;
        }
        if (sid != NULL && sid_to_utf8(sid, sid_text, sizeof(sid_text))) {
            emit_format(
                "W5_GATE120_DESCRIPTOR_ACE=INDEX=%lu|TYPE=%u|FLAGS=0x%02X|MASK=0x%08lX|SID=%s\n",
                (unsigned long)index,
                (unsigned int)header->AceType,
                (unsigned int)header->AceFlags,
                (unsigned long)mask,
                sid_text
            );
        } else {
            emit_format(
                "W5_GATE120_DESCRIPTOR_ACE=INDEX=%lu|TYPE=%u|FLAGS=0x%02X|MASK=0x%08lX|SID=UNAVAILABLE\n",
                (unsigned long)index,
                (unsigned int)header->AceType,
                (unsigned int)header->AceFlags,
                (unsigned long)mask
            );
        }
    }
}

static int run_ntopen(const wchar_t *object_text, ACCESS_MASK desired_access) {
    HANDLE handle = NULL;
    GATE120_NTSTATUS status = 0;
    GATE120_IO_STATUS_BLOCK io_status;
    emit_ascii("W5_GATE120_NTOPEN_STARTED=OBSERVED\n");
    if (!open_target(object_text, desired_access, &handle, &status, &io_status)) {
        emit_ascii("W5_GATE120_NTOPEN=UNAVAILABLE\n");
        return 0;
    }
    emit_hex32("W5_GATE120_NTOPEN_STATUS=", (ULONG)status);
    emit_hex32("W5_GATE120_NTOPEN_IO_STATUS=", (ULONG)io_status.Status);
    emit_hex64("W5_GATE120_NTOPEN_IO_INFORMATION=", io_status.Information);
    if ((ULONG)status == GATE120_STATUS_SUCCESS && handle != NULL &&
        handle != INVALID_HANDLE_VALUE) {
        emit_ascii("W5_GATE120_NTOPEN_HANDLE=PASS\n");
        emit_bool("W5_GATE120_NTOPEN_HANDLE_CLOSE=", CloseHandle(handle));
    }
    emit_ascii("W5_GATE120_NTOPEN_FINISHED=OBSERVED\n");
    return 0;
}

static int run_security(const wchar_t *object_text) {
    HANDLE handle = NULL;
    GATE120_NTSTATUS status = 0;
    GATE120_IO_STATUS_BLOCK io_status;
    HMODULE ntdll;
    gate120_nt_query_security_object_fn query_security;
    PSECURITY_DESCRIPTOR descriptor = NULL;
    DWORD required = 0;
    BOOL present = FALSE;
    BOOL defaulted = FALSE;
    PSID owner = NULL;
    PSID group = NULL;
    PACL dacl = NULL;
    emit_ascii("W5_GATE120_SECURITY_STARTED=OBSERVED\n");
    if (!open_target(
        object_text,
        GATE120_SYNCHRONIZE | GATE120_READ_CONTROL,
        &handle,
        &status,
        &io_status
    )) {
        emit_ascii("W5_GATE120_SECURITY_OPEN=UNAVAILABLE\n");
        return 0;
    }
    emit_hex32("W5_GATE120_SECURITY_OPEN_STATUS=", (ULONG)status);
    emit_hex32("W5_GATE120_SECURITY_IO_STATUS=", (ULONG)io_status.Status);
    emit_hex64("W5_GATE120_SECURITY_IO_INFORMATION=", io_status.Information);
    if ((ULONG)status != GATE120_STATUS_SUCCESS || handle == NULL ||
        handle == INVALID_HANDLE_VALUE) {
        emit_ascii("W5_GATE120_SECURITY_QUERY=NOT_ATTEMPTED\n");
        emit_ascii("W5_GATE120_SECURITY_FINISHED=OBSERVED\n");
        return 0;
    }
    emit_ascii("W5_GATE120_SECURITY_HANDLE=PASS\n");
    ntdll = GetModuleHandleW(L"ntdll.dll");
    query_security = ntdll == NULL
        ? NULL
        : (gate120_nt_query_security_object_fn)GetProcAddress(ntdll, "NtQuerySecurityObject");
    if (query_security == NULL) {
        emit_ascii("W5_GATE120_SECURITY_QUERY=UNAVAILABLE\n");
        emit_bool("W5_GATE120_SECURITY_HANDLE_CLOSE=", CloseHandle(handle));
        emit_ascii("W5_GATE120_SECURITY_FINISHED=OBSERVED\n");
        return 0;
    }
    status = query_security(
        handle,
        GATE120_SECURITY_OWNER | GATE120_SECURITY_GROUP | GATE120_SECURITY_DACL,
        NULL,
        0,
        &required
    );
    emit_hex32("W5_GATE120_SECURITY_QUERY_SIZE_STATUS=", (ULONG)status);
    emit_u32("W5_GATE120_SECURITY_QUERY_SIZE=", required);
    if (required == 0 || required > GATE120_MAX_BUFFER) {
        emit_hex32("W5_GATE120_SECURITY_QUERY_STATUS=", (ULONG)status);
        emit_bool("W5_GATE120_SECURITY_HANDLE_CLOSE=", CloseHandle(handle));
        emit_ascii("W5_GATE120_SECURITY_FINISHED=OBSERVED\n");
        return 0;
    }
    descriptor = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, required);
    if (descriptor == NULL) {
        emit_ascii("W5_GATE120_SECURITY_QUERY=OUT_OF_MEMORY\n");
        emit_bool("W5_GATE120_SECURITY_HANDLE_CLOSE=", CloseHandle(handle));
        emit_ascii("W5_GATE120_SECURITY_FINISHED=OBSERVED\n");
        return 0;
    }
    status = query_security(
        handle,
        GATE120_SECURITY_OWNER | GATE120_SECURITY_GROUP | GATE120_SECURITY_DACL,
        descriptor,
        required,
        &required
    );
    emit_hex32("W5_GATE120_SECURITY_QUERY_STATUS=", (ULONG)status);
    if ((ULONG)status == GATE120_STATUS_SUCCESS) {
        if (GetSecurityDescriptorOwner(descriptor, &owner, &defaulted)) {
            emit_descriptor_sid("OWNER", owner);
        }
        if (GetSecurityDescriptorGroup(descriptor, &group, &defaulted)) {
            emit_descriptor_sid("GROUP", group);
        }
        if (GetSecurityDescriptorDacl(descriptor, &present, &dacl, &defaulted)) {
            emit_format("W5_GATE120_SECURITY_DACL_PRESENT=%d\n", present ? 1 : 0);
            emit_format("W5_GATE120_SECURITY_DACL_NULL=%d\n", present && dacl == NULL ? 1 : 0);
            if (present && dacl != NULL) {
                emit_descriptor_aces(dacl);
            }
        }
    }
    HeapFree(GetProcessHeap(), 0, descriptor);
    emit_bool("W5_GATE120_SECURITY_HANDLE_CLOSE=", CloseHandle(handle));
    emit_ascii("W5_GATE120_SECURITY_FINISHED=OBSERVED\n");
    return 0;
}

static unsigned long long parse_u64(const wchar_t *text) {
    return _wcstoui64(text, NULL, 0);
}

int wmain(int argc, wchar_t **argv) {
    DWORD process_id = GetCurrentProcessId();
    if (argc < 3) {
        emit_ascii("W5_GATE120_ARGUMENTS=FAIL\n");
        return 30;
    }
    emit_format("W5_GATE120_PID=%lu\n", (unsigned long)process_id);
    emit_ascii("W5_GATE120_STARTED=OBSERVED\n");
    if (wcscmp(argv[1], L"ntopen") == 0 && argc >= 4) {
        (void)run_ntopen(argv[2], (ACCESS_MASK)parse_u64(argv[3]));
    } else if (wcscmp(argv[1], L"security") == 0) {
        (void)run_security(argv[2]);
    } else {
        emit_ascii("W5_GATE120_ARGUMENTS=FAIL\n");
    }
    emit_ascii("W5_GATE120_FINISHED=OBSERVED\n");
    return 0;
}
