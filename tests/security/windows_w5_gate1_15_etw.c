#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0602
#endif

#include <windows.h>
#include <evntcons.h>
#include <evntrace.h>
#include <evntprov.h>
#include <sddl.h>
#include <stdio.h>
#include <string.h>

#define SECURITY_MAX_SID_SIZE_VALUE 68
#define MAX_MARKER 512
#define TRACELOG_SECURITY_PATH \
    L"SYSTEM\\CurrentControlSet\\Control\\WMI\\Security"

typedef struct _TOKEN_SIDS {
    HANDLE token;
    PSID user;
    PSID logon;
    PSID synthetic;
    PSID world;
    PSID builtin_users;
    PSID authenticated_users;
    TOKEN_GROUPS *groups;
} TOKEN_SIDS;

static void token_sids_free(TOKEN_SIDS *sids);

static const GUID G1 = {
    0xca967c75,
    0x04bf,
    0x40b5,
    {0x9a, 0x16, 0x98, 0xb5, 0xf9, 0x33, 0x2a, 0x92}
};
static const GUID G2 = {
    0xb6fd710b,
    0xf783,
    0x4b1c,
    {0xab, 0x9c, 0xc6, 0x80, 0x99, 0xdc, 0xc0, 0xc7}
};
static const GUID G3 = {
    0xf3a71a4b,
    0x6118,
    0x4257,
    {0x8c, 0xcb, 0x39, 0xa3, 0x3b, 0xa0, 0x59, 0xd4}
};
static const GUID G4 = {
    0x703fcc13,
    0xb66f,
    0x5868,
    {0xdd, 0xd9, 0xe2, 0xdb, 0x7f, 0x38, 0x1f, 0xfb}
};
/* A deterministic, fresh Gate 1.15 control GUID.  It is never registered. */
static const GUID CONTROL = {
    0x6e8a7f3c,
    0x5f8d,
    0x4b42,
    {0x9b, 0x10, 0x4f, 0x6a, 0x1f, 0x15, 0x01, 0x15}
};

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

static void emit_labeled(const char *label, const char *suffix, const char *value) {
    char line[MAX_MARKER];
    (void)snprintf(line, sizeof(line), "W5_GATE115_DESC_%s_%s=%s\n", label, suffix, value);
    emit_ascii(line);
}

static void emit_labeled_u32(const char *label, const char *suffix, DWORD value) {
    char text[64];
    (void)snprintf(text, sizeof(text), "%lu", (unsigned long)value);
    emit_labeled(label, suffix, text);
}

static void emit_labeled_hex(const char *label, const char *suffix, ULONG value) {
    char text[64];
    (void)snprintf(text, sizeof(text), "0x%08lX", (unsigned long)value);
    emit_labeled(label, suffix, text);
}

static void emit_labeled_u64(const char *label, const char *suffix, unsigned long long value) {
    char text[64];
    (void)snprintf(text, sizeof(text), "%016llX", value);
    emit_labeled(label, suffix, text);
}

static void emit_status(const char *prefix, ULONG value) {
    emit_u32(prefix, value);
    {
        char line[256];
        char message[192];
        DWORD length = FormatMessageA(
            FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
            NULL,
            value,
            0,
            message,
            (DWORD)sizeof(message),
            NULL
        );
        if (length == 0) {
            emit_ascii("W5_GATE115_STATUS_MESSAGE=UNAVAILABLE\n");
            return;
        }
        while (length > 0 && (message[length - 1] == '\r' || message[length - 1] == '\n')) {
            --length;
        }
        message[length] = '\0';
        (void)snprintf(line, sizeof(line), "W5_GATE115_STATUS_MESSAGE=%s\n", message);
        emit_ascii(line);
    }
}

static const GUID *guid_for_label(const wchar_t *label) {
    if (_wcsicmp(label, L"G1") == 0) {
        return &G1;
    }
    if (_wcsicmp(label, L"G2") == 0) {
        return &G2;
    }
    if (_wcsicmp(label, L"G3") == 0) {
        return &G3;
    }
    if (_wcsicmp(label, L"G4") == 0) {
        return &G4;
    }
    if (_wcsicmp(label, L"CONTROL") == 0) {
        return &CONTROL;
    }
    return NULL;
}

static const wchar_t *registry_guid_name(const GUID *guid) {
    if (IsEqualGUID(guid, &G1)) {
        return L"{CA967C75-04BF-40B5-9A16-98B5F9332A92}";
    }
    if (IsEqualGUID(guid, &G2)) {
        return L"{B6FD710B-F783-4B1C-AB9C-C68099DCC0C7}";
    }
    if (IsEqualGUID(guid, &G3)) {
        return L"{F3A71A4B-6118-4257-8CCB-39A33BA059D4}";
    }
    if (IsEqualGUID(guid, &G4)) {
        return L"{703FCC13-B66F-5868-DDD9-E2DB7F381FFB}";
    }
    return L"{6E8A7F3C-5F8D-4B42-9B10-4F6A1F150115}";
}

static const char *label_for_index(int index) {
    static const char *labels[] = {"G1", "G2", "G3", "G4", "CONTROL"};
    if (index < 0 || index >= 5) {
        return NULL;
    }
    return labels[index];
}

static const GUID *guid_for_index(int index) {
    static const GUID *guids[] = {&G1, &G2, &G3, &G4, &CONTROL};
    if (index < 0 || index >= 5) {
        return NULL;
    }
    return guids[index];
}

static unsigned long long fnv1a64(const BYTE *data, DWORD length) {
    unsigned long long hash = 1469598103934665603ULL;
    DWORD index;
    for (index = 0; index < length; ++index) {
        hash ^= (unsigned long long)data[index];
        hash *= 1099511628211ULL;
    }
    return hash;
}

static PSID create_well_known(WELL_KNOWN_SID_TYPE type) {
    DWORD size = SECURITY_MAX_SID_SIZE_VALUE;
    PSID sid = HeapAlloc(GetProcessHeap(), 0, size);
    if (sid == NULL || !CreateWellKnownSid(type, NULL, sid, &size)) {
        if (sid != NULL) {
            HeapFree(GetProcessHeap(), 0, sid);
        }
        return NULL;
    }
    return sid;
}

static PSID copy_sid(PSID source) {
    DWORD size;
    PSID copy;
    if (source == NULL || !IsValidSid(source)) {
        return NULL;
    }
    size = GetLengthSid(source);
    copy = HeapAlloc(GetProcessHeap(), 0, size);
    if (copy == NULL || !CopySid(size, copy, source)) {
        if (copy != NULL) {
            HeapFree(GetProcessHeap(), 0, copy);
        }
        return NULL;
    }
    return copy;
}

static PSID token_user_sid(HANDLE token) {
    DWORD size = 0;
    TOKEN_USER *user;
    PSID result;
    if (GetTokenInformation(token, TokenUser, NULL, 0, &size) ||
        GetLastError() != ERROR_INSUFFICIENT_BUFFER || size == 0) {
        return NULL;
    }
    user = HeapAlloc(GetProcessHeap(), 0, size);
    if (user == NULL || !GetTokenInformation(token, TokenUser, user, size, &size)) {
        if (user != NULL) {
            HeapFree(GetProcessHeap(), 0, user);
        }
        return NULL;
    }
    result = copy_sid(user->User.Sid);
    HeapFree(GetProcessHeap(), 0, user);
    return result;
}

static PSID token_logon_sid(HANDLE token, TOKEN_GROUPS **groups_out) {
    DWORD size = 0;
    DWORD index;
    DWORD matches = 0;
    PSID found = NULL;
    TOKEN_GROUPS *groups;
    if (GetTokenInformation(token, TokenGroups, NULL, 0, &size) ||
        GetLastError() != ERROR_INSUFFICIENT_BUFFER || size == 0) {
        return NULL;
    }
    groups = HeapAlloc(GetProcessHeap(), 0, size);
    if (groups == NULL || !GetTokenInformation(token, TokenGroups, groups, size, &size)) {
        if (groups != NULL) {
            HeapFree(GetProcessHeap(), 0, groups);
        }
        return NULL;
    }
    for (index = 0; index < groups->GroupCount; ++index) {
        if ((groups->Groups[index].Attributes & SE_GROUP_LOGON_ID) == SE_GROUP_LOGON_ID) {
            ++matches;
            found = groups->Groups[index].Sid;
        }
    }
    if (matches != 1 || found == NULL) {
        HeapFree(GetProcessHeap(), 0, groups);
        return NULL;
    }
    *groups_out = groups;
    return copy_sid(found);
}

static BOOL token_sids_init(TOKEN_SIDS *sids, const wchar_t *synthetic_text) {
    BOOL valid;
    ZeroMemory(sids, sizeof(*sids));
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &sids->token)) {
        return FALSE;
    }
    sids->user = token_user_sid(sids->token);
    sids->logon = token_logon_sid(sids->token, &sids->groups);
    if (!ConvertStringSidToSidW(synthetic_text, &sids->synthetic)) {
        sids->synthetic = NULL;
    }
    sids->world = create_well_known(WinWorldSid);
    sids->builtin_users = create_well_known(WinBuiltinUsersSid);
    sids->authenticated_users = create_well_known(WinAuthenticatedUserSid);
    valid = sids->user != NULL && sids->logon != NULL && sids->synthetic != NULL &&
        sids->world != NULL && sids->builtin_users != NULL && sids->authenticated_users != NULL;
    if (!valid) {
        token_sids_free(sids);
    }
    return valid;
}

static void token_sids_free(TOKEN_SIDS *sids) {
    if (sids->token != NULL) {
        CloseHandle(sids->token);
    }
    if (sids->groups != NULL) {
        HeapFree(GetProcessHeap(), 0, sids->groups);
    }
    if (sids->user != NULL) {
        HeapFree(GetProcessHeap(), 0, sids->user);
    }
    if (sids->logon != NULL) {
        HeapFree(GetProcessHeap(), 0, sids->logon);
    }
    if (sids->synthetic != NULL) {
        LocalFree(sids->synthetic);
    }
    if (sids->world != NULL) {
        HeapFree(GetProcessHeap(), 0, sids->world);
    }
    if (sids->builtin_users != NULL) {
        HeapFree(GetProcessHeap(), 0, sids->builtin_users);
    }
    if (sids->authenticated_users != NULL) {
        HeapFree(GetProcessHeap(), 0, sids->authenticated_users);
    }
    ZeroMemory(sids, sizeof(*sids));
}

static const char *role_for_sid(PSID sid, const TOKEN_SIDS *sids) {
    if (EqualSid(sid, sids->world)) {
        return "WORLD";
    }
    if (EqualSid(sid, sids->user)) {
        return "TOKEN_USER";
    }
    if (EqualSid(sid, sids->logon)) {
        return "TOKEN_LOGON_SID";
    }
    if (EqualSid(sid, sids->synthetic)) {
        return "SYNTHETIC_WRITE";
    }
    if (EqualSid(sid, sids->builtin_users)) {
        return "BUILTIN_USERS";
    }
    if (EqualSid(sid, sids->authenticated_users)) {
        return "AUTHENTICATED_USERS";
    }
    return "OTHER";
}

static const char *ace_kind(BYTE type) {
    if (type == ACCESS_ALLOWED_ACE_TYPE) {
        return "ALLOW";
    }
    if (type == ACCESS_DENIED_ACE_TYPE) {
        return "DENY";
    }
    return "OTHER";
}

static void emit_ace_summary(const char *label, PSECURITY_DESCRIPTOR descriptor, const TOKEN_SIDS *sids) {
    BOOL present = FALSE;
    BOOL defaulted = FALSE;
    PACL dacl = NULL;
    ACL_SIZE_INFORMATION info;
    DWORD index;
    if (!GetSecurityDescriptorDacl(descriptor, &present, &dacl, &defaulted) || !present || dacl == NULL ||
        !GetAclInformation(dacl, &info, sizeof(info), AclSizeInformation)) {
        emit_labeled(label, "ACE_SUMMARY", "UNAVAILABLE");
        return;
    }
    emit_labeled_u32(label, "ACE_COUNT", info.AceCount);
    for (index = 0; index < info.AceCount && index < 32; ++index) {
        void *raw = NULL;
        ACE_HEADER *header;
        PSID sid;
        char text[256];
        if (!GetAce(dacl, index, &raw) || raw == NULL) {
            continue;
        }
        header = (ACE_HEADER *)raw;
        if (header->AceType == ACCESS_ALLOWED_ACE_TYPE || header->AceType == ACCESS_DENIED_ACE_TYPE) {
            ACCESS_ALLOWED_ACE *allowed = (ACCESS_ALLOWED_ACE *)raw;
            sid = &allowed->SidStart;
            (void)snprintf(
                text,
                sizeof(text),
                "%s:%u:0x%08lX:%s",
                ace_kind(header->AceType),
                (unsigned int)header->AceFlags,
                (unsigned long)allowed->Mask,
                role_for_sid(sid, sids)
            );
        } else {
            (void)snprintf(text, sizeof(text), "OTHER:%u:0x00000000:OTHER", (unsigned int)header->AceFlags);
        }
        {
            char suffix[64];
            (void)snprintf(suffix, sizeof(suffix), "ACE_%lu", (unsigned long)index);
            emit_labeled(label, suffix, text);
        }
    }
}

static BOOL query_descriptor(
    const char *label,
    const GUID *guid,
    PSECURITY_DESCRIPTOR *descriptor_out,
    DWORD *descriptor_size_out
) {
    ULONG first;
    ULONG second;
    ULONG size = 0;
    PSECURITY_DESCRIPTOR descriptor;
    first = EventAccessQuery((LPGUID)guid, NULL, &size);
    emit_labeled_hex(label, "AQ_FIRST", first);
    emit_labeled_u32(label, "AQ_SIZE_FIRST", size);
    if (first != ERROR_MORE_DATA && first != ERROR_SUCCESS) {
        *descriptor_out = NULL;
        *descriptor_size_out = 0;
        emit_labeled(label, "SD_VALID", "NO");
        return FALSE;
    }
    if (size == 0 || size > 1024 * 1024) {
        emit_labeled(label, "SD_VALID", "NO");
        return FALSE;
    }
    descriptor = HeapAlloc(GetProcessHeap(), 0, size);
    if (descriptor == NULL) {
        emit_labeled(label, "SD_VALID", "NO");
        return FALSE;
    }
    second = EventAccessQuery((LPGUID)guid, descriptor, &size);
    emit_labeled_hex(label, "AQ_SECOND", second);
    emit_labeled_u32(label, "AQ_SIZE_SECOND", size);
    if (second != ERROR_SUCCESS || !IsValidSecurityDescriptor(descriptor)) {
        HeapFree(GetProcessHeap(), 0, descriptor);
        emit_labeled(label, "SD_VALID", "NO");
        return FALSE;
    }
    emit_labeled(label, "SD_VALID", "YES");
    emit_labeled_u64(label, "SD_HASH_FNV1A64", fnv1a64((const BYTE *)descriptor, size));
    *descriptor_out = descriptor;
    *descriptor_size_out = size;
    return TRUE;
}

static BOOL registry_entry(const char *label, const GUID *guid) {
    HKEY root = NULL;
    HKEY subkey = NULL;
    LONG status;
    DWORD type = 0;
    DWORD size = 0;
    wchar_t names[2][64];
    const wchar_t *braced = registry_guid_name(guid);
    int index;
    (void)guid;
    (void)wcsncpy_s(
        names[0],
        sizeof(names[0]) / sizeof(names[0][0]),
        braced,
        _TRUNCATE
    );
    (void)wcsncpy_s(
        names[1],
        sizeof(names[1]) / sizeof(names[1][0]),
        names[0] + 1,
        _TRUNCATE
    );
    if (wcslen(names[1]) > 0 && names[1][wcslen(names[1]) - 1] == L'}') {
        names[1][wcslen(names[1]) - 1] = L'\0';
    }
    status = RegOpenKeyExW(HKEY_LOCAL_MACHINE, TRACELOG_SECURITY_PATH, 0, KEY_READ, &root);
    if (status != ERROR_SUCCESS) {
        emit_labeled(label, "REGISTRY", "ERROR");
        emit_labeled_u32(label, "REGISTRY_ERROR", (DWORD)status);
        return FALSE;
    }
    for (index = 0; index < 2; ++index) {
        type = 0;
        size = 0;
        status = RegQueryValueExW(root, names[index], NULL, &type, NULL, &size);
        if (status == ERROR_SUCCESS) {
            emit_labeled(label, "REGISTRY", "PRESENT");
            emit_labeled_u32(label, "REGISTRY_TYPE", type);
            emit_labeled_u32(label, "REGISTRY_LENGTH", size);
            RegCloseKey(root);
            return TRUE;
        }
    }
    status = RegOpenKeyExW(root, names[0], 0, KEY_READ, &subkey);
    if (status == ERROR_SUCCESS) {
        type = 0;
        size = 0;
        status = RegQueryValueExW(subkey, NULL, NULL, &type, NULL, &size);
        if (status == ERROR_SUCCESS) {
            emit_labeled(label, "REGISTRY", "PRESENT");
            emit_labeled_u32(label, "REGISTRY_TYPE", type);
            emit_labeled_u32(label, "REGISTRY_LENGTH", size);
            RegCloseKey(subkey);
            RegCloseKey(root);
            return TRUE;
        }
        RegCloseKey(subkey);
    }
    emit_labeled(label, "REGISTRY", "ABSENT");
    RegCloseKey(root);
    return FALSE;
}

static void access_check_summary(
    const char *label,
    PSECURITY_DESCRIPTOR descriptor,
    const TOKEN_SIDS *sids
) {
#if defined(TRACELOG_REGISTER_GUIDS)
    GENERIC_MAPPING mapping = {0, 0, 0, 0};
    PRIVILEGE_SET privileges;
    DWORD privilege_size = sizeof(privileges);
    DWORD granted = 0;
    BOOL access = FALSE;
    if (!AccessCheck(
        descriptor,
        sids->token,
        TRACELOG_REGISTER_GUIDS,
        &mapping,
        &privileges,
        &privilege_size,
        &granted,
        &access
    )) {
        emit_labeled(label, "ACCESSCHECK", "ERROR");
        emit_labeled_u32(label, "ACCESSCHECK_ERROR", GetLastError());
        emit_labeled(label, "ACCESSCHECK_MAPPING", "STATIC_ACCESSCHECK_MAPPING_UNCERTAIN");
        return;
    }
    emit_labeled(label, "ACCESSCHECK", access ? "PASS" : "DENY");
    emit_labeled_u32(label, "ACCESSCHECK_GRANTED", granted);
    emit_labeled(label, "ACCESSCHECK_MAPPING", "SDK_TRACELOG_REGISTER_GUIDS_ZERO_GENERIC");
#else
    (void)descriptor;
    (void)sids;
    emit_labeled(label, "ACCESSCHECK", "UNAVAILABLE");
    emit_labeled(label, "ACCESSCHECK_MAPPING", "STATIC_ACCESSCHECK_MAPPING_UNCERTAIN");
#endif
}

static void describe_one(const char *label, const GUID *guid, const wchar_t *synthetic_text) {
    PSECURITY_DESCRIPTOR descriptor = NULL;
    DWORD descriptor_size = 0;
    TOKEN_SIDS sids;
    BOOL registry_present = registry_entry(label, guid);
    BOOL descriptor_valid;
    descriptor_valid = query_descriptor(label, guid, &descriptor, &descriptor_size);
    emit_labeled(label, "SECURITY_SOURCE", registry_present ? "CUSTOM_REGISTRY_SECURITY" : "ETW_DEFAULT_EFFECTIVE_SECURITY");
    if (descriptor_valid) {
        if (token_sids_init(&sids, synthetic_text)) {
            emit_ace_summary(label, descriptor, &sids);
            access_check_summary(label, descriptor, &sids);
            token_sids_free(&sids);
        } else {
            emit_labeled(label, "ACE_SUMMARY", "TOKEN_CONTEXT_UNAVAILABLE");
        }
        HeapFree(GetProcessHeap(), 0, descriptor);
    }
    (void)descriptor_size;
}

static void event_register_one(const wchar_t *label, const wchar_t *synthetic_text) {
    const GUID *guid = guid_for_label(label);
    REGHANDLE handle = 0;
    ULONG status;
    TOKEN_SIDS sids;
    PSECURITY_DESCRIPTOR descriptor = NULL;
    DWORD descriptor_size = 0;
    if (guid == NULL) {
        emit_ascii("W5_GATE115_EVENT_ARGUMENTS=FAIL\n");
        return;
    }
    describe_one("CELL", guid, synthetic_text);
    if (token_sids_init(&sids, synthetic_text)) {
        if (query_descriptor("CELL", guid, &descriptor, &descriptor_size)) {
            access_check_summary("CELL", descriptor, &sids);
            HeapFree(GetProcessHeap(), 0, descriptor);
        }
        token_sids_free(&sids);
    }
    status = EventRegister(guid, NULL, NULL, &handle);
    emit_u32("W5_GATE115_EVENTREGISTER_RETURN=", status);
    emit_ascii("W5_GATE115_EVENTREGISTER_HANDLE=");
    emit_ascii(handle != 0 ? "NONZERO\n" : "ZERO\n");
    emit_status("W5_GATE115_EVENTREGISTER_STATUS=", status);
    if (status == ERROR_SUCCESS && handle != 0) {
        static const char provider_name[] = "NeuroCodeGate115";
        BYTE traits[2 + sizeof(provider_name)];
        USHORT traits_size = (USHORT)sizeof(traits);
        (void)memcpy(traits, &traits_size, sizeof(traits_size));
        (void)memcpy(traits + sizeof(traits_size), provider_name, sizeof(provider_name));
        status = EventSetInformation(
            handle,
            EventProviderSetTraits,
            traits,
            (ULONG)sizeof(traits)
        );
        emit_u32("W5_GATE115_PROVIDER_TRAITS_RETURN=", status);
        emit_status("W5_GATE115_PROVIDER_TRAITS_STATUS=", status);
        status = EventUnregister(handle);
        emit_u32("W5_GATE115_EVENTUNREGISTER_RETURN=", status);
        emit_ascii("W5_GATE115_EVENTUNREGISTER=PASS\n");
    } else {
        emit_ascii("W5_GATE115_PROVIDER_TRAITS=NOT_APPLICABLE\n");
        emit_ascii("W5_GATE115_EVENTUNREGISTER=NOT_APPLICABLE\n");
    }
}

int wmain(int argc, wchar_t **argv) {
    int index;
    emit_ascii("W5_GATE115_ETW_STARTED\n");
    if (argc < 2) {
        emit_ascii("W5_GATE115_ETW_ARGUMENTS=FAIL\n");
        emit_ascii("W5_GATE115_ETW_FINISHED\n");
        return 20;
    }
    if (_wcsicmp(argv[1], L"DESCRIBE") == 0) {
        if (argc < 3) {
            emit_ascii("W5_GATE115_ETW_ARGUMENTS=FAIL\n");
            emit_ascii("W5_GATE115_ETW_FINISHED\n");
            return 21;
        }
        emit_ascii("W5_GATE115_TRACELOG_MAPPING=");
#if defined(TRACELOG_REGISTER_GUIDS)
        emit_ascii("SDK_CONSTANT_PRESENT\n");
#else
        emit_ascii("STATIC_ACCESSCHECK_MAPPING_UNCERTAIN\n");
#endif
        for (index = 0; index < 5; ++index) {
            describe_one(label_for_index(index), guid_for_index(index), argv[2]);
        }
        emit_ascii("W5_GATE115_ETW_FINISHED\n");
        return 0;
    }
    if (_wcsicmp(argv[1], L"REGISTER") == 0 && argc >= 4) {
        emit_ascii("W5_GATE115_EVENTREGISTER_STARTED\n");
        event_register_one(argv[2], argv[3]);
        emit_ascii("W5_GATE115_EVENTREGISTER_FINISHED\n");
        emit_ascii("W5_GATE115_ETW_FINISHED\n");
        return 0;
    }
    emit_ascii("W5_GATE115_ETW_ARGUMENTS=FAIL\n");
    emit_ascii("W5_GATE115_ETW_FINISHED\n");
    return 22;
}
