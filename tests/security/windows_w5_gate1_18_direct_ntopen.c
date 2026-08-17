#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

#include <windows.h>
#include <stdio.h>
#include <wchar.h>

#pragma warning(disable: 4191)

typedef LONG GATE118_NTSTATUS;

typedef struct _GATE118_UNICODE_STRING {
    USHORT Length;
    USHORT MaximumLength;
    PWSTR Buffer;
} GATE118_UNICODE_STRING;

typedef struct _GATE118_OBJECT_ATTRIBUTES {
    ULONG Length;
    HANDLE RootDirectory;
    GATE118_UNICODE_STRING *ObjectName;
    ULONG Attributes;
    PVOID SecurityDescriptor;
    PVOID SecurityQualityOfService;
} GATE118_OBJECT_ATTRIBUTES;

typedef struct _GATE118_IO_STATUS_BLOCK {
    GATE118_NTSTATUS Status;
    ULONG Reserved;
    ULONG_PTR Information;
} GATE118_IO_STATUS_BLOCK;

typedef GATE118_NTSTATUS (NTAPI *gate118_nt_open_file_fn)(
    PHANDLE FileHandle,
    ACCESS_MASK DesiredAccess,
    GATE118_OBJECT_ATTRIBUTES *ObjectAttributes,
    GATE118_IO_STATUS_BLOCK *IoStatusBlock,
    ULONG ShareAccess,
    ULONG OpenOptions
);

static void emit_ascii(const char *text) {
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output != NULL && output != INVALID_HANDLE_VALUE) {
        (void)WriteFile(output, text, (DWORD)lstrlenA(text), &written, NULL);
    }
}

static void emit_u32(const char *prefix, ULONG value) {
    char line[128];
    (void)snprintf(line, sizeof(line), "%s0x%08lX\n", prefix, (unsigned long)value);
    emit_ascii(line);
}

static void emit_u64(const char *prefix, ULONG_PTR value) {
    char line[128];
    (void)snprintf(line, sizeof(line), "%s0x%llX\n", prefix, (unsigned long long)value);
    emit_ascii(line);
}

static unsigned long long parse_hex(const wchar_t *text) {
    return _wcstoui64(text, NULL, 0);
}

int wmain(int argc, wchar_t **argv) {
    HMODULE ntdll;
    gate118_nt_open_file_fn nt_open_file;
    GATE118_UNICODE_STRING object_name;
    GATE118_OBJECT_ATTRIBUTES object_attributes;
    GATE118_IO_STATUS_BLOCK io_status;
    HANDLE file_handle = NULL;
    GATE118_NTSTATUS status;
    size_t character_count;

    /*
     * argv[1]  exact captured effective object name
     * argv[2]  DesiredAccess
     * argv[3]  OBJECT_ATTRIBUTES.Attributes
     * argv[4]  ShareAccess
     * argv[5]  OpenOptions
     * argv[6]  UNICODE_STRING.Length
     * argv[7]  UNICODE_STRING.MaximumLength
     * argv[8]  OBJECT_ATTRIBUTES.Length
     */
    if (argc < 9) {
        emit_ascii("W5_GATE118_DIRECT_ARGUMENTS=FAIL\n");
        return 30;
    }

    character_count = wcslen(argv[1]);
    if (character_count > 0x7FFF || parse_hex(argv[6]) > 0xFFFE ||
        parse_hex(argv[7]) > 0xFFFE || parse_hex(argv[8]) > 0xFFFFFFFFULL) {
        emit_ascii("W5_GATE118_DIRECT_ARGUMENTS=FAIL\n");
        return 31;
    }

    object_name.Length = (USHORT)parse_hex(argv[6]);
    object_name.MaximumLength = (USHORT)parse_hex(argv[7]);
    object_name.Buffer = argv[1];
    ZeroMemory(&object_attributes, sizeof(object_attributes));
    object_attributes.Length = (ULONG)parse_hex(argv[8]);
    object_attributes.RootDirectory = NULL;
    object_attributes.ObjectName = &object_name;
    object_attributes.Attributes = (ULONG)parse_hex(argv[3]);
    ZeroMemory(&io_status, sizeof(io_status));

    ntdll = GetModuleHandleW(L"ntdll.dll");
    nt_open_file = ntdll == NULL
        ? NULL
        : (gate118_nt_open_file_fn)GetProcAddress(ntdll, "NtOpenFile");
    if (nt_open_file == NULL) {
        emit_ascii("W5_GATE118_DIRECT_NTOPEN=UNAVAILABLE\n");
        return 32;
    }

    emit_ascii("W5_GATE118_DIRECT_STARTED=OBSERVED\n");
    status = nt_open_file(
        &file_handle,
        (ACCESS_MASK)parse_hex(argv[2]),
        &object_attributes,
        &io_status,
        (ULONG)parse_hex(argv[4]),
        (ULONG)parse_hex(argv[5])
    );
    emit_u32("W5_GATE118_DIRECT_NTOPEN_STATUS=", (ULONG)status);
    emit_u64("W5_GATE118_DIRECT_FILE_HANDLE=", (ULONG_PTR)file_handle);
    emit_u32("W5_GATE118_DIRECT_IO_STATUS=", (ULONG)io_status.Status);
    emit_u64("W5_GATE118_DIRECT_IO_INFORMATION=", io_status.Information);
    if ((ULONG)status == 0 && file_handle != NULL && file_handle != INVALID_HANDLE_VALUE) {
        emit_ascii(CloseHandle(file_handle)
            ? "W5_GATE118_DIRECT_HANDLE_CLOSE=PASS\n"
            : "W5_GATE118_DIRECT_HANDLE_CLOSE=FAIL\n");
    }
    emit_ascii("W5_GATE118_DIRECT_FINISHED=OBSERVED\n");
    return (ULONG)status == 0 ? 0 : 24;
}
